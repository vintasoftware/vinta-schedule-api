import datetime
from unittest.mock import Mock

from django.db.utils import IntegrityError

import pytest
from model_bakery import baker

from organizations.models import Organization
from organizations.services import OrganizationService
from users.models import User


@pytest.mark.django_db
class TestOrganizationService:
    """Test suite for OrganizationService."""

    @pytest.fixture
    def user(self):
        """Create a test user."""
        return baker.make(User, email="test@example.com")

    @pytest.fixture
    def mock_calendar_service(self):
        """Create a mock CalendarService."""
        mock_service = Mock()
        mock_service.initialize_without_provider.return_value = None
        mock_service.request_organization_calendar_resources_import.return_value = None
        return mock_service

    @pytest.fixture
    def organization_service(self, mock_calendar_service):
        from di_core.containers import container

        """Create OrganizationService instance with mocked dependencies."""
        with container.calendar_service.override(mock_calendar_service):
            service = OrganizationService()
            yield service

    def test_create_organization_without_sync_rooms(
        self, organization_service, user, mock_calendar_service
    ):
        """Test creating an organization without room syncing."""
        organization_name = "Test Organization"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=False
        )

        # Verify organization was created correctly
        assert isinstance(organization, Organization)
        assert organization.name == organization_name
        assert organization.should_sync_rooms is False
        assert organization.id is not None

        # Verify organization exists in database
        db_organization = Organization.objects.get(id=organization.id)
        assert db_organization.name == organization_name
        assert db_organization.should_sync_rooms is False

        # Verify calendar service methods were not called when should_sync_rooms=False
        mock_calendar_service.initialize_without_provider.assert_not_called()
        mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()

    def test_create_organization_with_sync_rooms(
        self, organization_service, user, mock_calendar_service
    ):
        """Test creating an organization with room syncing enabled."""
        organization_name = "Test Organization with Rooms"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=True
        )

        # Verify organization was created correctly
        assert isinstance(organization, Organization)
        assert organization.name == organization_name
        assert organization.should_sync_rooms is True
        assert organization.id is not None

        # Verify organization exists in database
        db_organization = Organization.objects.get(id=organization.id)
        assert db_organization.name == organization_name
        assert db_organization.should_sync_rooms is True

        # Verify calendar service methods were called when should_sync_rooms=True
        mock_calendar_service.initialize_without_provider.assert_called_once_with(
            user_or_token=user, organization=organization
        )
        mock_calendar_service.request_organization_calendar_resources_import.assert_called_once()

        # Verify the import call was made with correct time range (365 days from now)
        call_args = mock_calendar_service.request_organization_calendar_resources_import.call_args
        start_time = call_args[1]["start_time"]
        end_time = call_args[1]["end_time"]

        # Check that start_time is approximately now (within 1 minute tolerance)
        now = datetime.datetime.now(tz=datetime.UTC)
        time_diff = abs((start_time - now).total_seconds())
        assert time_diff < 60, f"Start time should be close to now, but diff is {time_diff} seconds"

        # Check that end_time is approximately 365 days from start_time
        expected_end_time = start_time + datetime.timedelta(days=365)
        time_diff = abs((end_time - expected_end_time).total_seconds())
        assert (
            time_diff < 60
        ), f"End time should be 365 days from start time, but diff is {time_diff} seconds"

    def test_create_organization_default_sync_rooms_false(
        self, organization_service, user, mock_calendar_service
    ):
        """Test that should_sync_rooms defaults to False when not specified."""
        organization_name = "Test Organization Default"

        organization = organization_service.create_organization(
            creator=user, name=organization_name
        )

        # Verify organization was created with default should_sync_rooms=False
        assert organization.should_sync_rooms is False

        # Verify calendar service methods were not called
        mock_calendar_service.initialize_without_provider.assert_not_called()
        mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()

    def test_create_organization_sets_service_organization_attribute(
        self, organization_service, user, mock_calendar_service
    ):
        """Test that the service stores the created organization in self.organization."""
        organization_name = "Test Organization Attribute"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=False
        )

        # Verify that the service stores the organization instance
        assert hasattr(organization_service, "organization")
        assert organization_service.organization == organization
        assert organization_service.organization.name == organization_name

    def test_create_organization_multiple_calls(
        self, organization_service, user, mock_calendar_service
    ):
        """Test that multiple calls to create_organization work correctly."""
        # Create first organization
        org1 = organization_service.create_organization(
            creator=user, name="Organization 1", should_sync_rooms=False
        )

        # Create second organization
        with pytest.raises(IntegrityError):
            org2 = organization_service.create_organization(
                creator=user, name="Organization 2", should_sync_rooms=True
            )

        # Verify both organizations exist and are different
        assert org1.id != org2.id
        assert org1.name == "Organization 1"
        assert org2.name == "Organization 2"
        assert org1.should_sync_rooms is False
        assert org2.should_sync_rooms is True

        # Verify both exist in database
        assert Organization.objects.filter(id=org1.id).exists()
        assert Organization.objects.filter(id=org2.id).exists()

        # Verify the service stores the last created organization
        assert organization_service.organization == org2

    def test_create_organization_with_sync_rooms_calendar_service_exception(
        self, user, mock_calendar_service
    ):
        """Test behavior when calendar service raises an exception during room sync."""
        from di_core.containers import container

        # Configure mock to raise an exception
        mock_calendar_service.initialize_without_provider.side_effect = Exception(
            "Calendar service error"
        )

        with container.calendar_service.override(mock_calendar_service):
            service = OrganizationService()

            # The method should still raise the exception
            with pytest.raises(Exception, match="Calendar service error"):
                service.create_organization(
                    creator=user, name="Test Organization Exception", should_sync_rooms=True
                )

    @pytest.mark.parametrize("should_sync_rooms", [True, False])
    def test_create_organization_parametrized(
        self, organization_service, user, mock_calendar_service, should_sync_rooms
    ):
        """Parametrized test for both sync_rooms scenarios."""
        organization_name = f"Test Organization Sync={should_sync_rooms}"

        organization = organization_service.create_organization(
            creator=user, name=organization_name, should_sync_rooms=should_sync_rooms
        )

        # Verify organization was created correctly
        assert organization.should_sync_rooms == should_sync_rooms

        if should_sync_rooms:
            mock_calendar_service.initialize_without_provider.assert_called_once()
            mock_calendar_service.request_organization_calendar_resources_import.assert_called_once()
        else:
            mock_calendar_service.initialize_without_provider.assert_not_called()
            mock_calendar_service.request_organization_calendar_resources_import.assert_not_called()
