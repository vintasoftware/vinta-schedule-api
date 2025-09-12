import base64
from datetime import timedelta

from django.utils import timezone

import pytest

from calendar_integration.constants import EventManagementPermissions
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    CalendarManagementTokenPermission,
    ExternalAttendee,
)
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_ATTENDEE_PERMISSIONS,
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
    DEFAULT_EXTERNAL_ATTENDEE_PERMISSIONS,
    CalendarPermissionService,
)
from calendar_integration.services.dataclasses import (
    CalendarEventData,
    CalendarEventInputData,
    CalendarSettingsData,
    EventExternalAttendeeData,
    EventInternalAttendeeData,
)
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization
from users.models import Profile, User


@pytest.fixture
def organization(db):
    """Create a test organization."""
    return Organization.objects.create(name="Test Organization")


@pytest.fixture
def user(db):
    """Create a test user."""
    user = User.objects.create(email="test@example.com")
    Profile.objects.create(user=user)
    return user


@pytest.fixture
def another_user(db):
    """Create another test user."""
    import uuid

    unique_email = f"another+{uuid.uuid4().hex[:8]}@example.com"
    user = User.objects.create(email=unique_email, username=unique_email)
    Profile.objects.create(user=user)
    return user


@pytest.fixture
def calendar(db, organization):
    """Create a test calendar."""
    return Calendar.objects.create(
        name="Test Calendar",
        organization=organization,
    )


@pytest.fixture
def event(db, calendar, organization):
    """Create a test event."""
    return CalendarEvent.objects.create(
        calendar=calendar,
        organization=organization,
        title="Test Event",
        description="Test event description",
        start_time_tz_unaware=timezone.now(),
        end_time_tz_unaware=timezone.now() + timedelta(hours=1),
        timezone="UTC",
    )


@pytest.fixture
def external_attendee(db, organization):
    """Create an external attendee."""
    return ExternalAttendee.objects.create(
        email="external@example.com",
        name="External User",
        organization=organization,
    )


@pytest.fixture
def permission_service():
    """Create a CalendarPermissionService instance."""
    return CalendarPermissionService()


@pytest.fixture
def calendar_token(db, calendar, user, organization):
    """Create a calendar management token."""
    token_str = generate_long_lived_token()
    hashed_token = hash_long_lived_token(token_str)

    token = CalendarManagementToken.objects.create(
        calendar_fk=calendar,
        user=user,
        token_hash=hashed_token,
        organization=organization,
    )

    # Add default calendar owner permissions
    for permission in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token,
            permission=permission,
            organization=organization,
        )

    return token, token_str


@pytest.fixture
def event_token(db, event, user, organization):
    """Create an event management token."""
    token_str = generate_long_lived_token()
    hashed_token = hash_long_lived_token(token_str)

    token = CalendarManagementToken.objects.create(
        event_fk=event,
        user=user,
        token_hash=hashed_token,
        organization=organization,
    )

    # Add default attendee permissions
    for permission in DEFAULT_ATTENDEE_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token,
            permission=permission,
            organization=organization,
        )

    return token, token_str


@pytest.fixture
def external_event_token(db, event, external_attendee, organization):
    """Create an external attendee event management token."""
    token_str = generate_long_lived_token()
    hashed_token = hash_long_lived_token(token_str)

    token = CalendarManagementToken.objects.create(
        event_fk=event,
        external_attendee_fk=external_attendee,
        token_hash=hashed_token,
        organization=organization,
    )

    # Add default external attendee permissions
    for permission in DEFAULT_EXTERNAL_ATTENDEE_PERMISSIONS:
        CalendarManagementTokenPermission.objects.create(
            token_fk=token,
            permission=permission,
            organization=organization,
        )

    return token, token_str


class TestCalendarPermissionServiceInitialization:
    """Tests for service initialization methods."""

    def test_initialize_with_valid_token(self, permission_service, event_token, organization):
        """Test successful initialization with a valid token."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()

        permission_service.initialize_with_token(token_b64, organization.id)

        assert permission_service.token == token
        assert permission_service.token.event_fk is not None

    def test_initialize_with_invalid_token_id(self, permission_service, organization):
        """Test initialization with invalid token ID."""
        invalid_token = "99999:invalid_token_string"
        token_b64 = base64.b64encode(invalid_token.encode()).decode()

        with pytest.raises(ValueError, match="Invalid token string provided"):
            permission_service.initialize_with_token(token_b64, organization.id)

    def test_initialize_with_invalid_token_string(
        self, permission_service, event_token, organization
    ):
        """Test initialization with invalid token string."""
        token, _ = event_token
        invalid_token = f"{token.id}:invalid_token_string"
        token_b64 = base64.b64encode(invalid_token.encode()).decode()

        with pytest.raises(ValueError, match="Invalid token string provided"):
            permission_service.initialize_with_token(token_b64, organization.id)

    def test_initialize_with_revoked_token(self, permission_service, event_token, organization):
        """Test initialization with a revoked token."""
        token, token_str = event_token
        token.revoked_at = timezone.now()
        token.save()

        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()

        with pytest.raises(ValueError, match="Invalid token string provided"):
            permission_service.initialize_with_token(token_b64, organization.id)

    def test_initialize_with_user_and_event(
        self, permission_service, user, event_token, organization
    ):
        """Test successful initialization with user and event ID."""
        token, _ = event_token

        permission_service.initialize_with_user(user, organization.id, event_id=token.event_fk.id)

        assert permission_service.token == token

    def test_initialize_with_user_and_calendar(
        self, permission_service, user, calendar_token, organization
    ):
        """Test successful initialization with user and calendar ID."""
        token, _ = calendar_token

        permission_service.initialize_with_user(
            user, organization.id, calendar_id=token.calendar_fk.id
        )

        assert permission_service.token == token

    def test_initialize_with_user_both_event_and_calendar(
        self, permission_service, user, organization
    ):
        """Test initialization fails when both event_id and calendar_id are provided."""
        with pytest.raises(ValueError, match="Specify either calendar_id or event_id, not both"):
            permission_service.initialize_with_user(
                user, organization.id, event_id=1, calendar_id=1
            )

    def test_initialize_with_user_neither_event_nor_calendar(
        self, permission_service, user, organization
    ):
        """Test initialization fails when neither event_id nor calendar_id are provided."""
        with pytest.raises(ValueError, match="Either calendar_id or event_id must be specified"):
            permission_service.initialize_with_user(user, organization.id)

    def test_initialize_with_user_nonexistent_token(self, permission_service, user, organization):
        """Test initialization fails when token doesn't exist for user."""
        with pytest.raises(ValueError, match="Error initializing CalendarPermissionCheckService"):
            permission_service.initialize_with_user(user, organization.id, event_id=99999)


class TestCalendarPermissionServicePermissionChecking:
    """Tests for permission checking methods."""

    def test_has_permission_with_valid_permission(
        self, permission_service, event_token, organization
    ):
        """Test checking for a permission that the token has."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()

        permission_service.initialize_with_token(token_b64, organization.id)

        assert permission_service.has_permission(EventManagementPermissions.UPDATE_ATTENDEES)
        assert permission_service.has_permission(EventManagementPermissions.RESCHEDULE)
        assert permission_service.has_permission(EventManagementPermissions.CANCEL)

    def test_has_permission_with_invalid_permission(
        self, permission_service, event_token, organization
    ):
        """Test checking for a permission that the token doesn't have."""
        token, token_str = event_token
        # Remove CREATE permission (which is not in DEFAULT_ATTENDEE_PERMISSIONS)
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()

        permission_service.initialize_with_token(token_b64, organization.id)

        assert not permission_service.has_permission(EventManagementPermissions.CREATE)

    def test_has_permission_update_self_rsvp_with_update_attendees(
        self, permission_service, event_token, organization
    ):
        """Test that UPDATE_ATTENDEES permission allows UPDATE_SELF_RSVP."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()

        permission_service.initialize_with_token(token_b64, organization.id)

        # Should work because token has UPDATE_ATTENDEES permission
        assert permission_service.has_permission(EventManagementPermissions.UPDATE_SELF_RSVP)

    def test_has_permission_without_initialization(self, permission_service):
        """Test that checking permissions without initialization raises error."""
        with pytest.raises(ValueError, match="Service not initialized"):
            permission_service.has_permission(EventManagementPermissions.CREATE)


class TestCalendarPermissionServiceAttendancePermissions:
    """Tests for attendance-related permission checking."""

    def test_check_attendances_update_add_internal_attendee(
        self, permission_service, event_token, organization, another_user
    ):
        """Test that adding an internal attendee requires UPDATE_ATTENDEES permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_attendances = []
        new_attendances = [
            EventInternalAttendeeData(
                user_id=another_user.id,
                email=another_user.email,
                name=another_user.get_full_name(),
                status="pending",
            )
        ]

        required_permission = permission_service._check_attendances_update_necessary_permissions(
            old_attendances, [], new_attendances, []
        )

        assert required_permission == EventManagementPermissions.UPDATE_ATTENDEES

    def test_check_attendances_update_remove_self_internal_attendee(
        self, permission_service, event_token, organization, user
    ):
        """Test that removing self as internal attendee requires UPDATE_SELF_RSVP permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_attendances = [
            EventInternalAttendeeData(
                user_id=user.id, email=user.email, name=user.get_full_name(), status="accepted"
            )
        ]
        new_attendances = []

        required_permission = permission_service._check_attendances_update_necessary_permissions(
            old_attendances, [], new_attendances, []
        )

        assert required_permission == EventManagementPermissions.UPDATE_SELF_RSVP

    def test_check_attendances_update_remove_other_internal_attendee(
        self, permission_service, event_token, organization, another_user
    ):
        """Test that removing another user as internal attendee requires UPDATE_ATTENDEES permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_attendances = [
            EventInternalAttendeeData(
                user_id=another_user.id,
                email=another_user.email,
                name=another_user.get_full_name(),
                status="accepted",
            )
        ]
        new_attendances = []

        required_permission = permission_service._check_attendances_update_necessary_permissions(
            old_attendances, [], new_attendances, []
        )

        assert required_permission == EventManagementPermissions.UPDATE_ATTENDEES

    def test_check_attendances_update_add_external_attendee(
        self, permission_service, event_token, organization
    ):
        """Test that adding an external attendee requires UPDATE_ATTENDEES permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_external_attendances = []
        new_external_attendances = [
            EventExternalAttendeeData(
                email="new@example.com", name="New External", status="pending"
            )
        ]

        required_permission = permission_service._check_attendances_update_necessary_permissions(
            [], old_external_attendances, [], new_external_attendances
        )

        assert required_permission == EventManagementPermissions.UPDATE_ATTENDEES

    def test_check_attendances_update_remove_self_external_attendee(
        self, permission_service, external_event_token, organization, external_attendee
    ):
        """Test that removing self as external attendee requires UPDATE_SELF_RSVP permission."""
        token, token_str = external_event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_external_attendances = [
            EventExternalAttendeeData(
                email=external_attendee.email, name=external_attendee.name, status="accepted"
            )
        ]
        new_external_attendances = []

        required_permission = permission_service._check_attendances_update_necessary_permissions(
            [], old_external_attendances, [], new_external_attendances
        )

        assert required_permission == EventManagementPermissions.UPDATE_SELF_RSVP

    def test_check_attendances_update_no_changes(
        self, permission_service, event_token, organization
    ):
        """Test that no attendance changes return None."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        attendances = [
            EventInternalAttendeeData(
                user_id=1, email="test@example.com", name="Test User", status="accepted"
            )
        ]

        required_permission = permission_service._check_attendances_update_necessary_permissions(
            attendances, [], attendances, []
        )

        assert required_permission is None


class TestCalendarPermissionServiceEventDetailsPermissions:
    """Tests for event details permission checking."""

    def test_check_event_details_update_title_change(
        self, permission_service, event_token, organization
    ):
        """Test that changing event title requires UPDATE_DETAILS permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        required_permission = permission_service._check_event_details_update_necessary_permissions(
            "Old Title", "Old Description", "New Title", "Old Description"
        )

        assert required_permission == EventManagementPermissions.UPDATE_DETAILS

    def test_check_event_details_update_description_change(
        self, permission_service, event_token, organization
    ):
        """Test that changing event description requires UPDATE_DETAILS permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        required_permission = permission_service._check_event_details_update_necessary_permissions(
            "Same Title", "Old Description", "Same Title", "New Description"
        )

        assert required_permission == EventManagementPermissions.UPDATE_DETAILS

    def test_check_event_details_update_no_changes(
        self, permission_service, event_token, organization
    ):
        """Test that no details changes return None."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        required_permission = permission_service._check_event_details_update_necessary_permissions(
            "Same Title", "Same Description", "Same Title", "Same Description"
        )

        assert required_permission is None


class TestCalendarPermissionServiceSchedulePermissions:
    """Tests for event schedule permission checking."""

    def test_check_event_reschedule_start_time_change(
        self, permission_service, event_token, organization
    ):
        """Test that changing start time requires RESCHEDULE permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_start = timezone.now()
        old_end = old_start + timedelta(hours=1)
        new_start = old_start + timedelta(hours=1)
        new_end = old_end

        required_permission = permission_service._check_event_reschedule_necessary_permissions(
            old_start, old_end, new_start, new_end
        )

        assert required_permission == EventManagementPermissions.RESCHEDULE

    def test_check_event_reschedule_end_time_change(
        self, permission_service, event_token, organization
    ):
        """Test that changing end time requires RESCHEDULE permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_start = timezone.now()
        old_end = old_start + timedelta(hours=1)
        new_start = old_start
        new_end = old_end + timedelta(hours=1)

        required_permission = permission_service._check_event_reschedule_necessary_permissions(
            old_start, old_end, new_start, new_end
        )

        assert required_permission == EventManagementPermissions.RESCHEDULE

    def test_check_event_reschedule_no_changes(self, permission_service, event_token, organization):
        """Test that no schedule changes return None."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_start = timezone.now()
        old_end = old_start + timedelta(hours=1)

        required_permission = permission_service._check_event_reschedule_necessary_permissions(
            old_start, old_end, old_start, old_end
        )

        assert required_permission is None


class TestCalendarPermissionServiceCanPerformUpdate:
    """Tests for the can_perform_update method."""

    def create_event_data(self, event, title="Test Event", description="Test Description"):
        """Helper to create CalendarEventData."""
        return CalendarEventData(
            id=event.id,
            calendar_id=event.calendar_fk_id,
            title=title,
            description=description,
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            timezone="UTC",
            attendees=[],
            external_attendees=[],
            resources=[],
            recurrence_rule=None,
            external_id="test_external_id",
            calendar_settings=None,
            status="confirmed",
            is_recurring=False,
            recurring_event_id=None,
            original_payload=None,
        )

    def test_can_perform_update_with_sufficient_permissions(
        self, permission_service, event_token, organization, event
    ):
        """Test that update is allowed when user has sufficient permissions."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_event = self.create_event_data(event, title="Old Title")
        new_event = self.create_event_data(event, title="New Title")

        assert permission_service.can_perform_update(old_event, new_event)

    def test_can_perform_update_with_insufficient_permissions(
        self, permission_service, event_token, organization, event
    ):
        """Test that update is denied when user lacks necessary permissions."""
        token, token_str = event_token
        # Remove UPDATE_DETAILS permission
        token.permissions.filter(permission=EventManagementPermissions.UPDATE_DETAILS).delete()

        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_event = self.create_event_data(event, title="Old Title")
        new_event = self.create_event_data(event, title="New Title")

        assert not permission_service.can_perform_update(old_event, new_event)

    def test_can_perform_update_wrong_event(
        self, permission_service, event_token, organization, event
    ):
        """Test that update is denied when token is for different event."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        # Create event data for a different event
        old_event = self.create_event_data(event, title="Old Title")
        old_event.id = 99999  # Different event ID
        new_event = self.create_event_data(event, title="New Title")
        new_event.id = 99999

        assert not permission_service.can_perform_update(old_event, new_event)

    def test_can_perform_update_cancellation(
        self, permission_service, event_token, organization, event
    ):
        """Test that cancellation requires CANCEL permission."""
        token, token_str = event_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_event = self.create_event_data(event)

        # Cancellation (new_event = None)
        assert permission_service.can_perform_update(old_event, None)

    def test_can_perform_update_cancellation_without_permission(
        self, permission_service, event_token, organization, event
    ):
        """Test that cancellation is denied without CANCEL permission."""
        token, token_str = event_token
        # Remove CANCEL permission
        token.permissions.filter(permission=EventManagementPermissions.CANCEL).delete()

        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        old_event = self.create_event_data(event)

        # Cancellation (new_event = None)
        assert not permission_service.can_perform_update(old_event, None)


class TestCalendarPermissionServiceCanPerformScheduling:
    """Tests for the can_perform_scheduling method."""

    def test_can_perform_scheduling_with_public_calendar(self, permission_service):
        """Test that scheduling is allowed on public calendars without authentication."""
        calendar_settings = CalendarSettingsData(
            accepts_public_scheduling=True, manage_available_windows=False
        )
        event_data = CalendarEventInputData(
            title="Test Event",
            description="Test Description",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            timezone="UTC",
        )

        assert permission_service.can_perform_scheduling(1, calendar_settings, event_data)

    def test_can_perform_scheduling_with_calendar_token(
        self, permission_service, calendar_token, organization
    ):
        """Test that scheduling is allowed with calendar CREATE permission."""
        token, token_str = calendar_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        calendar_settings = CalendarSettingsData(
            accepts_public_scheduling=False, manage_available_windows=False
        )
        event_data = CalendarEventInputData(
            title="Test Event",
            description="Test Description",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            timezone="UTC",
        )

        assert permission_service.can_perform_scheduling(
            token.calendar_fk.id, calendar_settings, event_data
        )

    def test_can_perform_scheduling_without_permission(
        self, permission_service, calendar_token, organization
    ):
        """Test that scheduling is denied without CREATE permission."""
        token, token_str = calendar_token
        # Remove CREATE permission
        token.permissions.filter(permission=EventManagementPermissions.CREATE).delete()

        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        calendar_settings = CalendarSettingsData(
            accepts_public_scheduling=False, manage_available_windows=False
        )
        event_data = CalendarEventInputData(
            title="Test Event",
            description="Test Description",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            timezone="UTC",
        )

        assert not permission_service.can_perform_scheduling(
            token.calendar_fk.id, calendar_settings, event_data
        )

    def test_can_perform_scheduling_wrong_calendar(
        self, permission_service, calendar_token, organization
    ):
        """Test that scheduling is denied for different calendar."""
        token, token_str = calendar_token
        token_id_and_str = f"{token.id}:{token_str}"
        token_b64 = base64.b64encode(token_id_and_str.encode()).decode()
        permission_service.initialize_with_token(token_b64, organization.id)

        calendar_settings = CalendarSettingsData(
            accepts_public_scheduling=False, manage_available_windows=False
        )
        event_data = CalendarEventInputData(
            title="Test Event",
            description="Test Description",
            start_time=timezone.now(),
            end_time=timezone.now() + timedelta(hours=1),
            timezone="UTC",
        )

        # Use different calendar ID
        assert not permission_service.can_perform_scheduling(99999, calendar_settings, event_data)


class TestCalendarPermissionServiceTokenCreation:
    """Tests for token creation methods."""

    def test_create_calendar_owner_token(self, permission_service, user, calendar, organization):
        """Test creating calendar owner token with default permissions."""
        token = permission_service.create_calendar_owner_token(organization.id, user, calendar.id)

        assert token.calendar_fk == calendar
        assert token.user == user
        assert token.organization == organization

        # Check default permissions
        permission_values = list(token.permissions.values_list("permission", flat=True))
        assert set(permission_values) == set(DEFAULT_CALENDAR_OWNER_PERMISSIONS)

    def test_create_calendar_owner_token_custom_permissions(
        self, permission_service, user, calendar, organization
    ):
        """Test creating calendar owner token with custom permissions."""
        custom_permissions = [EventManagementPermissions.CREATE, EventManagementPermissions.CANCEL]

        token = permission_service.create_calendar_owner_token(
            organization.id, user, calendar.id, permissions=custom_permissions
        )

        permission_values = list(token.permissions.values_list("permission", flat=True))
        assert set(permission_values) == set(custom_permissions)

    def test_create_calendar_owner_token_empty_permissions(
        self, permission_service, user, calendar, organization
    ):
        """Test that creating token with empty permissions raises error."""
        with pytest.raises(ValueError, match="At least one permission must be specified"):
            permission_service.create_calendar_owner_token(
                organization.id, user, calendar.id, permissions=[]
            )

    def test_create_attendee_token(self, permission_service, user, event, organization):
        """Test creating attendee token with default permissions."""
        token = permission_service.create_attendee_token(organization.id, user, event.id)

        assert token.event_fk == event
        assert token.user == user
        assert token.organization == organization
        assert token.token_hash is not None

        # Check default permissions
        permission_values = list(token.permissions.values_list("permission", flat=True))
        assert set(permission_values) == set(DEFAULT_ATTENDEE_PERMISSIONS)

    def test_create_attendee_token_custom_permissions(
        self, permission_service, user, event, organization
    ):
        """Test creating attendee token with custom permissions."""
        custom_permissions = [EventManagementPermissions.UPDATE_SELF_RSVP]

        token = permission_service.create_attendee_token(
            organization.id, user, event.id, permissions=custom_permissions
        )

        permission_values = list(token.permissions.values_list("permission", flat=True))
        assert set(permission_values) == set(custom_permissions)

    def test_create_external_attendee_update_token(
        self, permission_service, event, external_attendee, organization
    ):
        """Test creating external attendee update token."""
        token = permission_service.create_external_attendee_update_token(
            organization.id, event.id, external_attendee.id
        )

        assert token.event_fk == event
        assert token.external_attendee_fk == external_attendee
        assert token.organization == organization
        assert token.token_hash is not None

        # Check default permissions
        permission_values = list(token.permissions.values_list("permission", flat=True))
        assert set(permission_values) == set(DEFAULT_EXTERNAL_ATTENDEE_PERMISSIONS)

    def test_create_external_attendee_schedule_token(
        self, permission_service, calendar, external_attendee, organization
    ):
        """Test creating external attendee schedule token."""
        token = permission_service.create_external_attendee_schedule_token(
            organization.id, calendar.id, external_attendee.id
        )

        assert token.calendar_fk == calendar
        assert token.external_attendee_fk == external_attendee
        assert token.organization == organization
        assert token.token_hash is not None

        # Should only have CREATE permission
        permission_values = list(token.permissions.values_list("permission", flat=True))
        assert permission_values == [EventManagementPermissions.CREATE]


class TestCalendarPermissionServiceConstants:
    """Tests for permission constants."""

    def test_default_calendar_owner_permissions(self):
        """Test that default calendar owner permissions include expected values."""
        expected_permissions = {
            EventManagementPermissions.CREATE,
            EventManagementPermissions.UPDATE_ATTENDEES,
            EventManagementPermissions.UPDATE_DETAILS,
            EventManagementPermissions.RESCHEDULE,
            EventManagementPermissions.CANCEL,
        }

        assert set(DEFAULT_CALENDAR_OWNER_PERMISSIONS) == expected_permissions

    def test_default_attendee_permissions(self):
        """Test that default attendee permissions include expected values."""
        expected_permissions = {
            EventManagementPermissions.UPDATE_ATTENDEES,
            EventManagementPermissions.UPDATE_DETAILS,
            EventManagementPermissions.RESCHEDULE,
            EventManagementPermissions.CANCEL,
        }

        assert set(DEFAULT_ATTENDEE_PERMISSIONS) == expected_permissions

    def test_default_external_attendee_permissions(self):
        """Test that default external attendee permissions include expected values."""
        expected_permissions = {
            EventManagementPermissions.UPDATE_SELF_RSVP,
            EventManagementPermissions.RESCHEDULE,
            EventManagementPermissions.CANCEL,
        }

        assert set(DEFAULT_EXTERNAL_ATTENDEE_PERMISSIONS) == expected_permissions
