import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from organizations.models import Organization, OrganizationMembership
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.models import WebhookConfiguration, WebhookEvent


User = get_user_model()


def assert_response_status_code(response, expected_status_code):
    """Helper function to assert response status with useful error message."""
    assert response.status_code == expected_status_code, (
        f"The status error {response.status_code} != {expected_status_code}\n"
        f"Response Payload: {json.dumps(response.json() if hasattr(response, 'json') else str(response.content))}"
    )


class WebhookTestFactory:
    @staticmethod
    def create_organization(name="Test Organization"):
        return baker.make(Organization, name=name)

    @staticmethod
    def create_organization_membership(user, organization):
        return baker.make(OrganizationMembership, user=user, organization=organization)

    @staticmethod
    def create_webhook_configuration(
        organization=None,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/webhook",
        headers=None,
        deleted_at=None,
    ):
        if organization is None:
            organization = WebhookTestFactory.create_organization()
        if headers is None:
            headers = {"Content-Type": "application/json"}

        return baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=event_type,
            url=url,
            headers=headers,
            deleted_at=deleted_at,
        )

    @staticmethod
    def create_webhook_event(
        organization=None,
        configuration=None,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/webhook",
        status=WebhookStatus.PENDING,
        headers=None,
        payload=None,
        retry_number=None,
        main_event=None,
    ):
        if organization is None:
            organization = WebhookTestFactory.create_organization()
        if configuration is None:
            configuration = WebhookTestFactory.create_webhook_configuration(
                organization=organization
            )
        if headers is None:
            headers = {"Content-Type": "application/json"}
        if payload is None:
            payload = {"test": "data"}

        return baker.make(
            WebhookEvent,
            organization=organization,
            configuration=configuration,
            event_type=event_type,
            url=url,
            status=status,
            headers=headers,
            payload=payload,
            retry_number=retry_number,
            main_event=main_event,
        )


@pytest.fixture
def organization(user):
    organization = WebhookTestFactory.create_organization()
    WebhookTestFactory.create_organization_membership(user, organization)
    return organization


@pytest.fixture
def webhook_configuration(organization):
    return WebhookTestFactory.create_webhook_configuration(organization=organization)


@pytest.fixture
def webhook_event(organization, webhook_configuration):
    return WebhookTestFactory.create_webhook_event(
        organization=organization,
        configuration=webhook_configuration,
    )


@pytest.mark.django_db
class TestWebhookConfigurationViewSet:
    """Test suite for WebhookConfigurationViewSet."""

    def test_list_webhook_configurations_authenticated(self, auth_client, webhook_configuration):
        """Test listing webhook configurations for authenticated user."""
        url = reverse("api:WebhookConfigurations-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert len(data["results"]) == 1
        assert data["results"][0]["id"] == webhook_configuration.id
        assert data["results"][0]["event_type"] == webhook_configuration.event_type
        assert data["results"][0]["url"] == webhook_configuration.url

    def test_list_webhook_configurations_unauthenticated(
        self, anonymous_client, webhook_configuration
    ):
        """Test listing webhook configurations for unauthenticated user."""
        url = reverse("api:WebhookConfigurations-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_list_webhook_configurations_filters_by_organization(
        self, auth_client, user, webhook_configuration
    ):
        """Test that webhook configurations are filtered by user's organization."""
        # Create configuration for different organization
        other_org = WebhookTestFactory.create_organization(name="Other Organization")
        other_config = WebhookTestFactory.create_webhook_configuration(organization=other_org)

        url = reverse("api:WebhookConfigurations-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        # Should only see configurations from user's organization
        assert len(data["results"]) == 1
        assert data["results"][0]["id"] == webhook_configuration.id
        assert data["results"][0]["id"] != other_config.id

    def test_list_webhook_configurations_excludes_deleted(self, auth_client, organization):
        """Test that deleted webhook configurations are excluded from list."""
        # Create active configuration
        active_config = WebhookTestFactory.create_webhook_configuration(organization=organization)

        # Create deleted configuration
        import datetime

        WebhookTestFactory.create_webhook_configuration(
            organization=organization, deleted_at=datetime.datetime.now(tz=datetime.UTC)
        )

        url = reverse("api:WebhookConfigurations-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        # Should only see active configuration
        assert len(data["results"]) == 1
        assert data["results"][0]["id"] == active_config.id

    def test_retrieve_webhook_configuration(self, auth_client, webhook_configuration):
        """Test retrieving a specific webhook configuration."""
        url = reverse("api:WebhookConfigurations-detail", kwargs={"pk": webhook_configuration.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert data["id"] == webhook_configuration.id
        assert data["event_type"] == webhook_configuration.event_type
        assert data["url"] == webhook_configuration.url
        assert data["headers"] == webhook_configuration.headers

    def test_retrieve_nonexistent_webhook_configuration(self, auth_client, organization):
        """Test retrieving a non-existent webhook configuration from user's organization."""
        # Create a configuration first, then delete it to get a valid ID that doesn't exist in the org
        config = WebhookTestFactory.create_webhook_configuration(organization=organization)
        non_existent_id = config.id
        config.delete()  # Hard delete to ensure it doesn't exist

        url = reverse("api:WebhookConfigurations-detail", kwargs={"pk": non_existent_id})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_create_webhook_configuration(self, auth_client, organization):
        """Test creating a new webhook configuration."""
        url = reverse("api:WebhookConfigurations-list")
        data = {
            "event_type": WebhookEventType.CALENDAR_EVENT_CREATED,
            "url": "https://example.com/new-webhook",
            "headers": {"Authorization": "Bearer token"},
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_201_CREATED)
        response_data = response.json()

        assert response_data["event_type"] == data["event_type"]
        assert response_data["url"] == data["url"]
        assert response_data["headers"] == data["headers"]

        # Verify configuration was created in database
        config = WebhookConfiguration.objects.filter(
            id=response_data["id"], organization=organization
        ).first()
        assert config is not None
        assert config.organization == organization
        assert config.event_type == data["event_type"]
        assert config.url == data["url"]

    def test_create_webhook_configuration_validation_errors(self, auth_client):
        """Test webhook configuration creation with validation errors."""
        url = reverse("api:WebhookConfigurations-list")
        data = {
            "event_type": "invalid_event_type",
            "url": "not-a-url",
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_update_webhook_configuration(self, auth_client, webhook_configuration):
        """Test updating a webhook configuration."""
        url = reverse("api:WebhookConfigurations-detail", kwargs={"pk": webhook_configuration.pk})
        data = {
            "event_type": WebhookEventType.CALENDAR_EVENT_UPDATED,
            "url": "https://example.com/updated-webhook",
            "headers": {"Authorization": "Bearer new-token"},
        }

        response = auth_client.put(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()

        assert response_data["event_type"] == data["event_type"]
        assert response_data["url"] == data["url"]
        assert response_data["headers"] == data["headers"]

    def test_partial_update_webhook_configuration(self, auth_client, webhook_configuration):
        """Test partially updating a webhook configuration."""
        url = reverse("api:WebhookConfigurations-detail", kwargs={"pk": webhook_configuration.pk})
        data = {"url": "https://example.com/partially-updated-webhook"}

        response = auth_client.patch(url, data, format="json")

        assert_response_status_code(response, status.HTTP_200_OK)
        response_data = response.json()

        assert response_data["url"] == data["url"]
        # Other fields should remain unchanged
        assert response_data["event_type"] == webhook_configuration.event_type

    def test_delete_webhook_configuration(self, auth_client, webhook_configuration):
        """Test deleting a webhook configuration (soft delete)."""
        url = reverse("api:WebhookConfigurations-detail", kwargs={"pk": webhook_configuration.pk})

        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_204_NO_CONTENT)

        # Verify configuration is soft deleted by querying the database directly
        # Use .get() with primary key to bypass organization filtering for this check
        # Get the pk before it might be modified
        config_id = webhook_configuration.pk

        # Query the database directly to check if deleted_at was set
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT deleted_at FROM webhooks_webhookconfiguration WHERE id = %s", [config_id]
            )
            result = cursor.fetchone()
            assert result is not None
            assert result[0] is not None  # deleted_at should be set


@pytest.mark.django_db
class TestWebhookEventViewSet:
    """Test suite for WebhookEventViewSet."""

    def test_list_webhook_events_authenticated(self, auth_client, webhook_event):
        """Test listing webhook events for authenticated user."""
        url = reverse("api:WebhookEvents-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert len(data["results"]) == 1
        assert data["results"][0]["event_type"] == webhook_event.event_type
        assert data["results"][0]["url"] == webhook_event.url
        assert data["results"][0]["status"] == webhook_event.status

    def test_list_webhook_events_unauthenticated(self, anonymous_client, webhook_event):
        """Test listing webhook events for unauthenticated user."""
        url = reverse("api:WebhookEvents-list")
        response = anonymous_client.get(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_list_webhook_events_filters_by_organization(self, auth_client, user, webhook_event):
        """Test that webhook events are filtered by user's organization."""
        # Create event for different organization
        other_org = WebhookTestFactory.create_organization(name="Other Organization")
        WebhookTestFactory.create_webhook_event(organization=other_org)

        url = reverse("api:WebhookEvents-list")
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        # Should only see events from user's organization
        assert len(data["results"]) == 1
        assert data["results"][0]["event_type"] == webhook_event.event_type

    def test_retrieve_webhook_event(self, auth_client, webhook_event):
        """Test retrieving a specific webhook event."""
        url = reverse("api:WebhookEvents-detail", kwargs={"pk": webhook_event.pk})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert data["event_type"] == webhook_event.event_type
        assert data["url"] == webhook_event.url
        assert data["status"] == webhook_event.status
        assert data["payload"] == webhook_event.payload

    def test_retrieve_nonexistent_webhook_event(self, auth_client, organization):
        """Test retrieving a non-existent webhook event from user's organization."""
        # Create an event first, then delete it to get a valid ID that doesn't exist in the org
        event = WebhookTestFactory.create_webhook_event(organization=organization)
        non_existent_id = event.id
        event.delete()  # Hard delete to ensure it doesn't exist

        url = reverse("api:WebhookEvents-detail", kwargs={"pk": non_existent_id})
        response = auth_client.get(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_create_webhook_event_not_allowed(self, auth_client):
        """Test that creating webhook events is not allowed."""
        url = reverse("api:WebhookEvents-list")
        data = {
            "event_type": WebhookEventType.CALENDAR_EVENT_CREATED,
            "url": "https://example.com/webhook",
            "payload": {"test": "data"},
        }

        response = auth_client.post(url, data, format="json")

        assert_response_status_code(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_update_webhook_event_not_allowed(self, auth_client, webhook_event):
        """Test that updating webhook events is not allowed."""
        url = reverse("api:WebhookEvents-detail", kwargs={"pk": webhook_event.pk})
        data = {"status": WebhookStatus.SUCCESS}

        response = auth_client.put(url, data, format="json")

        assert_response_status_code(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_delete_webhook_event_not_allowed(self, auth_client, webhook_event):
        """Test that deleting webhook events is not allowed."""
        url = reverse("api:WebhookEvents-detail", kwargs={"pk": webhook_event.pk})

        response = auth_client.delete(url)

        assert_response_status_code(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    @patch("webhooks.services.WebhookService.schedule_event_retry")
    def test_retry_failed_webhook_event(self, mock_schedule_retry, auth_client, organization):
        """Test retrying a failed webhook event."""
        # Create failed webhook event
        failed_event = WebhookTestFactory.create_webhook_event(
            organization=organization,
            status=WebhookStatus.FAILED,
            retry_number=1,
        )

        # Mock successful retry scheduling
        retry_event = WebhookTestFactory.create_webhook_event(
            organization=organization,
            status=WebhookStatus.PENDING,
            retry_number=2,
            main_event=failed_event,
        )
        mock_schedule_retry.return_value = retry_event

        url = reverse("api:WebhookEvents-retry", kwargs={"pk": failed_event.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_200_OK)
        data = response.json()

        assert data["status"] == WebhookStatus.PENDING
        assert data["retry_number"] == 2

        # Verify service was called with correct parameters
        mock_schedule_retry.assert_called_once_with(
            event=failed_event,
            use_current_configuration=True,
            is_manual=True,
        )

    @patch("webhooks.services.WebhookService.schedule_event_retry")
    def test_retry_non_failed_webhook_event_error(
        self, mock_schedule_retry, auth_client, organization
    ):
        """Test that retrying a non-failed webhook event returns error."""
        # Create successful webhook event
        success_event = WebhookTestFactory.create_webhook_event(
            organization=organization,
            status=WebhookStatus.SUCCESS,
        )

        url = reverse("api:WebhookEvents-retry", kwargs={"pk": success_event.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        data = response.json()

        assert "Only failed events can be retried" in data["error"]

        # Verify service was not called
        mock_schedule_retry.assert_not_called()

    @patch("webhooks.services.WebhookService.schedule_event_retry")
    def test_retry_webhook_event_max_retries_reached(
        self, mock_schedule_retry, auth_client, organization
    ):
        """Test retrying a webhook event when max retries are reached."""
        # Create failed webhook event
        failed_event = WebhookTestFactory.create_webhook_event(
            organization=organization,
            status=WebhookStatus.FAILED,
            retry_number=5,
        )

        # Mock service returning None (max retries reached)
        mock_schedule_retry.return_value = None

        url = reverse("api:WebhookEvents-retry", kwargs={"pk": failed_event.pk})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        data = response.json()

        assert "Maximum retry limit reached" in data["error"]

    def test_retry_nonexistent_webhook_event(self, auth_client, organization):
        """Test retrying a non-existent webhook event from user's organization."""
        # Create an event first, then delete it to get a valid ID that doesn't exist in the org
        event = WebhookTestFactory.create_webhook_event(organization=organization)
        non_existent_id = event.id
        event.delete()  # Hard delete to ensure it doesn't exist

        url = reverse("api:WebhookEvents-retry", kwargs={"pk": non_existent_id})
        response = auth_client.post(url)

        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_retry_webhook_event_unauthenticated(self, anonymous_client, webhook_event):
        """Test retrying a webhook event without authentication."""
        url = reverse("api:WebhookEvents-retry", kwargs={"pk": webhook_event.pk})
        response = anonymous_client.post(url)

        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)
