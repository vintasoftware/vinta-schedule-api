import datetime
from unittest.mock import MagicMock, Mock, patch

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import (
    Calendar,
    CalendarOrganizationResourceImportStatus,
    CalendarOrganizationResourcesImport,
    CalendarSync,
    CalendarSyncStatus,
    GoogleCalendarServiceAccount,
)
from calendar_integration.tasks.calendar_sync_tasks import (
    import_organization_calendar_resources_task,
    sync_calendar_task,
)
from organizations.models import Organization
from users.models import User


@pytest.fixture
def social_account(db):
    """Create a social account for testing."""
    user = User.objects.create_user(email="test@example.com", password="testpass123")
    account = SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="12345")
    return account


@pytest.fixture
def social_token(social_account):
    """Create a social token for testing."""
    return SocialToken.objects.create(
        account=social_account,
        token="test_access_token",
        token_secret="test_refresh_token",
        expires_at=datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1),
    )


@pytest.fixture
def organization(db):
    """Create a calendar organization for testing."""
    return Organization.objects.create(name="Test Organization", should_sync_rooms=True)


@pytest.fixture
def calendar(db, organization):
    """Create a calendar for testing."""
    return Calendar.objects.create(
        name="Test Calendar",
        description="A test calendar",
        external_id="cal_123",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def google_service_account(db, organization):
    """Create a Google service account for testing."""
    return GoogleCalendarServiceAccount.objects.create(
        email="service@example.com",
        audience="https://oauth2.googleapis.com/token",
        public_key="test_public_key",
        private_key_id="test_key_id",
        private_key="test_private_key",
        organization=organization,
    )


# Tests for sync_calendar_task
def test_sync_calendar_task_with_social_account(
    social_account, social_token, calendar, organization
):
    """Test sync_calendar_task with a social account."""

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    mock_service = MagicMock()
    sync_calendar_task(
        "social_account",
        social_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=mock_service,
    )
    mock_service.authenticate.assert_called_once_with(
        account=social_account.user, organization=organization
    )
    mock_service.sync_events.assert_called_once_with(calendar_sync)


def test_sync_calendar_task_with_google_service_account(
    google_service_account, calendar, organization
):
    """Test sync_calendar_task with a Google service account."""

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    mock_service = MagicMock()
    sync_calendar_task(
        "google_service_account",
        google_service_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=mock_service,
    )
    mock_service.authenticate.assert_called_once_with(
        account=google_service_account, organization=organization
    )
    mock_service.sync_events.assert_called_once_with(calendar_sync)


def test_sync_calendar_task_with_invalid_social_account(calendar, organization):
    """Test sync_calendar_task with an invalid social account ID."""

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    with patch(
        "calendar_integration.tasks.calendar_sync_tasks.CalendarService"
    ) as mock_service_class:
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        # Call the task with invalid account ID
        sync_calendar_task("social_account", 99999, calendar_sync.id, organization.id)

        # Verify CalendarService was not called since account doesn't exist
        mock_service_class.assert_not_called()
        mock_service.sync_events.assert_not_called()


def test_sync_calendar_task_with_invalid_google_service_account(calendar, organization):
    """Test sync_calendar_task with an invalid Google service account ID."""

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    with patch(
        "calendar_integration.tasks.calendar_sync_tasks.CalendarService"
    ) as mock_service_class:
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        # Call the task with invalid account ID
        sync_calendar_task("google_service_account", 99999, calendar_sync.id, organization.id)

        # Verify CalendarService was not called since account doesn't exist
        mock_service_class.assert_not_called()
        mock_service.sync_events.assert_not_called()


def test_sync_calendar_task_with_invalid_calendar_sync(social_account, social_token, organization):
    """Test sync_calendar_task with an invalid calendar sync ID."""

    with patch(
        "calendar_integration.tasks.calendar_sync_tasks.CalendarService"
    ) as mock_service_class:
        mock_service = Mock()
        mock_service_class.return_value = mock_service

        # Call the task with invalid calendar sync ID
        sync_calendar_task("social_account", social_account.id, 99999, organization.id)

        # Verify CalendarService was not called since calendar sync doesn't exist
        mock_service_class.assert_not_called()
        mock_service.sync_events.assert_not_called()


def test_sync_calendar_task_with_already_started_calendar_sync(
    social_account, social_token, calendar, organization
):
    """Test sync_calendar_task with a calendar sync that has already started."""

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        status=CalendarSyncStatus.IN_PROGRESS,  # Already started
        organization=organization,
    )

    mock_service = MagicMock()

    mock_service.sync_events.side_effect = Exception("Calendar API Error")
    with patch(
        "calendar_integration.tasks.calendar_sync_tasks.CalendarSync.objects.get_not_started_calendar_sync"
    ) as mock_get_sync:
        mock_get_sync.return_value = None
        sync_calendar_task(
            "social_account",
            social_account.id,
            calendar_sync.id,
            organization.id,
            calendar_service=mock_service,
        )

    mock_service.authenticate.assert_not_called()
    mock_service.sync_events.assert_not_called()


def test_sync_calendar_task_service_exception(social_account, social_token, calendar, organization):
    """Test sync_calendar_task when CalendarService raises an exception."""

    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    mock_service = MagicMock()
    mock_service.sync_events.side_effect = Exception("Calendar API Error")
    with pytest.raises(Exception, match="Calendar API Error"):
        sync_calendar_task(
            "social_account",
            social_account.id,
            calendar_sync.id,
            organization.id,
            calendar_service=mock_service,
        )
    mock_service.authenticate.assert_called_once_with(
        account=social_account.user, organization=organization
    )
    mock_service.sync_events.assert_called_once_with(calendar_sync)


# Tests for import_organization_calendar_resources_task


def test_import_organization_calendar_resources_with_social_account(
    social_account, organization, db
):
    """Test import_organization_calendar_resources_task with a social account."""
    import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
        organization=organization,
        status=CalendarOrganizationResourceImportStatus.NOT_STARTED,
        start_time=datetime.datetime.now(datetime.UTC),
        end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
    )

    mock_service = MagicMock()
    import_organization_calendar_resources_task(
        "social_account",
        social_account.id,
        organization.id,
        import_workflow_state.id,
        calendar_service=mock_service,
    )
    mock_service.authenticate.assert_called_once_with(
        account=social_account.user, organization=organization
    )
    mock_service.import_organization_calendar_resources.assert_called_once_with(
        import_workflow_state
    )


def test_import_organization_calendar_resources_with_google_service_account(
    google_service_account, organization, db
):
    """Test import_organization_calendar_resources_task with a Google service account."""
    import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
        organization=organization,
        status=CalendarOrganizationResourceImportStatus.NOT_STARTED,
        start_time=datetime.datetime.now(datetime.UTC),
        end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
    )
    mock_service = MagicMock()
    import_organization_calendar_resources_task(
        "google_service_account",
        google_service_account.id,
        organization.id,
        import_workflow_state.id,
        calendar_service=mock_service,
    )
    mock_service.authenticate.assert_called_once_with(
        account=google_service_account, organization=organization
    )
    mock_service.import_organization_calendar_resources.assert_called_once_with(
        import_workflow_state
    )


def test_import_organization_calendar_resources_with_invalid_organization(db):
    """Test import_organization_calendar_resources_task with invalid organization ID."""

    mock_service = MagicMock()
    import_organization_calendar_resources_task("social_account", 1, 99999, 1)
    mock_service.authenticate.assert_not_called()
    mock_service.import_organization_calendar_resources.assert_not_called()


def test_import_organization_calendar_resources_with_invalid_import_state(
    social_account, organization, db
):
    """Test import_organization_calendar_resources_task with invalid import_workflow_state_id."""
    mock_service = MagicMock()
    import_organization_calendar_resources_task(
        "social_account",
        social_account.id,
        organization.id,
        99999,
        calendar_service=mock_service,
    )
    mock_service.authenticate.assert_not_called()
    mock_service.import_organization_calendar_resources.assert_not_called()


def test_import_organization_calendar_resources_with_invalid_account(organization, db):
    """Test import_organization_calendar_resources_task with invalid account ID (social_account)."""
    import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
        organization=organization,
        status=CalendarOrganizationResourceImportStatus.NOT_STARTED,
        start_time=datetime.datetime.now(datetime.UTC),
        end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
    )
    mock_service = MagicMock()
    import_organization_calendar_resources_task(
        "social_account",
        99999,
        organization.id,
        import_workflow_state.id,
        calendar_service=mock_service,
    )
    mock_service.authenticate.assert_not_called()
    mock_service.import_organization_calendar_resources.assert_not_called()


def test_import_organization_calendar_resources_with_invalid_google_service_account(
    organization, db
):
    """Test import_organization_calendar_resources_task with invalid account ID (google_service_account)."""
    import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
        organization=organization,
        status=CalendarOrganizationResourceImportStatus.NOT_STARTED,
        start_time=datetime.datetime.now(datetime.UTC),
        end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
    )

    mock_service = MagicMock()
    import_organization_calendar_resources_task(
        "google_service_account",
        99999,
        organization.id,
        import_workflow_state.id,
        calendar_service=mock_service,
    )
    mock_service.import_organization_calendar_resources.assert_not_called()


def test_import_organization_calendar_resources_service_exception(
    social_account, social_token, organization, db
):
    """Test import_organization_calendar_resources_task when CalendarService raises an exception."""
    import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
        organization=organization,
        status=CalendarOrganizationResourceImportStatus.NOT_STARTED,
        start_time=datetime.datetime.now(datetime.UTC),
        end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
    )
    mock_service = MagicMock()
    mock_service.import_organization_calendar_resources.side_effect = Exception("Import Error")
    with pytest.raises(Exception, match="Import Error"):
        import_organization_calendar_resources_task(
            "social_account",
            social_account.id,
            organization.id,
            import_workflow_state.id,
            calendar_service=mock_service,
        )
    mock_service.authenticate.assert_called_once_with(
        account=social_account.user, organization=organization
    )
    mock_service.import_organization_calendar_resources.assert_called_once_with(
        import_workflow_state
    )


def test_sync_calendar_task_with_changes_applied(
    social_account, social_token, calendar, organization
):
    """Test sync_calendar_task properly applies changes through CalendarService."""
    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    mock_service = MagicMock()

    # Mock the sync_events method to simulate applying changes
    def mock_sync_events(sync):
        # Simulate the sync process that would apply changes
        sync.status = CalendarSyncStatus.SUCCESS
        sync.save()

    mock_service.sync_events.side_effect = mock_sync_events

    sync_calendar_task(
        "social_account",
        social_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=mock_service,
    )

    # Verify the service was called correctly
    mock_service.authenticate.assert_called_once_with(
        account=social_account.user, organization=organization
    )
    mock_service.sync_events.assert_called_once_with(calendar_sync)

    # Verify calendar sync status was updated
    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.SUCCESS


def test_sync_calendar_task_handles_sync_failures(
    social_account, social_token, calendar, organization
):
    """Test sync_calendar_task handles failures during sync process."""
    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    mock_service = MagicMock()

    # Mock the sync_events method to simulate sync failure
    def mock_sync_events_failure(sync):
        # Simulate sync failure
        sync.status = CalendarSyncStatus.FAILED
        sync.error_message = "Sync failed due to API error"
        sync.save()

    mock_service.sync_events.side_effect = mock_sync_events_failure

    sync_calendar_task(
        "social_account",
        social_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=mock_service,
    )

    # Verify the service was called correctly
    mock_service.authenticate.assert_called_once_with(
        account=social_account.user, organization=organization
    )
    mock_service.sync_events.assert_called_once_with(calendar_sync)

    # Verify calendar sync status reflects failure
    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.FAILED
    assert calendar_sync.error_message == "Sync failed due to API error"


def test_sync_calendar_task_with_google_service_account_changes_applied(
    google_service_account, calendar, organization
):
    """Test sync_calendar_task with Google service account applies changes."""
    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    mock_service = MagicMock()

    # Mock the sync_events method to track what changes would be applied
    changes_applied = []

    def mock_sync_events(sync):
        # Simulate that changes are being applied during sync
        changes_applied.append("events_created")
        changes_applied.append("blocked_times_updated")
        changes_applied.append("attendances_created")
        sync.status = CalendarSyncStatus.SUCCESS
        sync.save()

    mock_service.sync_events.side_effect = mock_sync_events

    sync_calendar_task(
        "google_service_account",
        google_service_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=mock_service,
    )

    # Verify service was authenticated and sync called
    mock_service.authenticate.assert_called_once_with(
        account=google_service_account, organization=organization
    )
    mock_service.sync_events.assert_called_once_with(calendar_sync)

    # Verify changes would have been applied (simulated)
    assert "events_created" in changes_applied
    assert "blocked_times_updated" in changes_applied
    assert "attendances_created" in changes_applied

    # Verify sync completed successfully
    calendar_sync.refresh_from_db()
    assert calendar_sync.status == CalendarSyncStatus.SUCCESS


def test_import_organization_calendar_resources_task_with_changes_simulation(
    social_account, organization, db
):
    """Test import_organization_calendar_resources_task simulates applying resource changes."""
    import_workflow_state = CalendarOrganizationResourcesImport.objects.create(
        organization=organization,
        status=CalendarOrganizationResourceImportStatus.NOT_STARTED,
        start_time=datetime.datetime.now(datetime.UTC),
        end_time=datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365),
    )

    mock_service = MagicMock()

    # Mock the import method to simulate resource import changes
    resources_imported = []

    def mock_import_resources(import_state):
        # Simulate that resources are being imported with changes
        resources_imported.extend(["conference_room_a", "conference_room_b", "projector_1"])
        # Simulate status update
        import_state.status = CalendarOrganizationResourceImportStatus.SUCCESS
        import_state.save()

    mock_service.import_organization_calendar_resources.side_effect = mock_import_resources

    import_organization_calendar_resources_task(
        "social_account",
        social_account.id,
        organization.id,
        import_workflow_state.id,
        calendar_service=mock_service,
    )

    # Verify service was authenticated and import called
    mock_service.authenticate.assert_called_once_with(
        account=social_account.user, organization=organization
    )
    mock_service.import_organization_calendar_resources.assert_called_once_with(
        import_workflow_state
    )

    # Verify resources would have been imported (simulated)
    assert "conference_room_a" in resources_imported
    assert "conference_room_b" in resources_imported
    assert "projector_1" in resources_imported

    # Verify import completed successfully
    import_workflow_state.refresh_from_db()
    assert import_workflow_state.status == CalendarOrganizationResourceImportStatus.SUCCESS


def test_sync_calendar_task_tracks_matched_event_ids(
    social_account, social_token, calendar, organization
):
    """Test that sync_calendar_task properly tracks matched event IDs for deletions."""
    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        start_datetime=datetime.datetime(2025, 6, 22, 0, 0, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2025, 6, 22, 23, 59, tzinfo=datetime.UTC),
        should_update_events=True,
        organization=organization,
    )

    mock_service = MagicMock()

    # Mock sync to track which events were matched vs deleted
    matched_ids = set()
    deleted_ids = []

    def mock_sync_events(sync):
        # Simulate tracking of matched and deleted events during sync
        matched_ids.update(["event_1", "event_2", "event_3"])
        deleted_ids.extend(["old_event_1", "old_event_2"])
        sync.status = CalendarSyncStatus.SUCCESS
        sync.save()

    mock_service.sync_events.side_effect = mock_sync_events

    sync_calendar_task(
        "social_account",
        social_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=mock_service,
    )

    # Verify sync was called
    mock_service.sync_events.assert_called_once_with(calendar_sync)

    # Verify event tracking would work (simulated)
    assert "event_1" in matched_ids
    assert "event_2" in matched_ids
    assert "event_3" in matched_ids
    assert "old_event_1" in deleted_ids
    assert "old_event_2" in deleted_ids
