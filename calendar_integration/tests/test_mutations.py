"""Tests for calendar integration mutations."""

from collections.abc import Callable
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest
from graphql import GraphQLError
from model_bakery import baker

from calendar_integration.models import Calendar
from calendar_integration.mutations import (
    CalendarWebhookMutations,
    CleanupWebhookEventsInput,
    CreateWebhookSubscriptionInput,
    DeleteWebhookSubscriptionInput,
    RefreshWebhookSubscriptionInput,
    WebhookCleanupResult,
    WebhookDeleteResult,
    WebhookMutationDependencies,
    WebhookSubscriptionResult,
    get_webhook_mutation_dependencies,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.webhook_analytics_service import WebhookAnalyticsService
from organizations.models import Organization


class TestGetWebhookMutationDependencies:
    """Test cases for get_webhook_mutation_dependencies function."""

    def test_get_dependencies_success(self) -> None:
        """Test successfully getting dependencies."""
        mock_calendar_service: CalendarService = Mock()

        deps = get_webhook_mutation_dependencies(calendar_service=mock_calendar_service)

        assert isinstance(deps, WebhookMutationDependencies)
        assert deps.calendar_service == mock_calendar_service

    def test_get_dependencies_missing_service(self) -> None:
        """Test getting dependencies with missing calendar service."""
        with pytest.raises(GraphQLError, match="Missing required dependency"):
            get_webhook_mutation_dependencies(calendar_service=None)

    def test_get_dependencies_default_none(self) -> None:
        """Test getting dependencies with default None values."""
        with pytest.raises(GraphQLError) as exc_info:
            get_webhook_mutation_dependencies(calendar_service=None)

        assert "Missing required dependency" in str(exc_info.value)


@pytest.mark.django_db
class TestCalendarWebhookMutations:
    """Test cases for CalendarWebhookMutations class."""

    def test_create_webhook_subscription_success(self) -> None:
        """Test creating webhook subscription successfully."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)
        calendar = baker.make(Calendar, organization=organization)

        input_data = CreateWebhookSubscriptionInput(
            organization_id=organization.id, calendar_id=calendar.id
        )

        mock_service = Mock()
        mock_subscription = Mock()
        mock_service.create_calendar_webhook_subscription.return_value = mock_subscription
        mock_deps = WebhookMutationDependencies(calendar_service=mock_service)

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookSubscriptionResult,
                cast(Callable[..., Any], mutations.create_webhook_subscription)(input_data),
            )

            assert result.success is True
            assert result.subscription == mock_subscription
            assert result.error_message is None
            mock_service.create_calendar_webhook_subscription.assert_called_once_with(
                calendar=calendar
            )

    def test_create_webhook_subscription_organization_not_found(self) -> None:
        """Test creating webhook subscription with non-existent organization."""
        mutations = CalendarWebhookMutations()

        # Use a non-existent organization ID
        input_data = CreateWebhookSubscriptionInput(organization_id=99999, calendar_id=1)

        mock_deps = WebhookMutationDependencies(calendar_service=Mock())

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookSubscriptionResult,
                cast(Callable[..., Any], mutations.create_webhook_subscription)(input_data),
            )

            assert result.success is False
            assert result.subscription is None
            assert result.error_message == "Organization not found"

    def test_create_webhook_subscription_calendar_not_found(self) -> None:
        """Test creating webhook subscription with non-existent calendar."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        # Use a non-existent calendar ID
        input_data = CreateWebhookSubscriptionInput(
            organization_id=organization.id, calendar_id=99999
        )

        mock_deps = WebhookMutationDependencies(calendar_service=Mock())

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookSubscriptionResult,
                cast(Callable[..., Any], mutations.create_webhook_subscription)(input_data),
            )

            assert result.success is False
            assert result.subscription is None
            assert result.error_message == "Calendar not found"

    def test_create_webhook_subscription_service_error(self) -> None:
        """Test creating webhook subscription with service error."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)
        calendar = baker.make(Calendar, organization=organization)

        input_data = CreateWebhookSubscriptionInput(
            organization_id=organization.id, calendar_id=calendar.id
        )

        mock_service = Mock()
        mock_service.create_calendar_webhook_subscription.side_effect = ValueError("Service error")
        mock_deps = WebhookMutationDependencies(calendar_service=mock_service)

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookSubscriptionResult,
                cast(Callable[..., Any], mutations.create_webhook_subscription)(input_data),
            )

            assert result.success is False
            assert result.subscription is None
            assert result.error_message is not None
            assert "Failed to create subscription: Service error" in result.error_message

    def test_delete_webhook_subscription_success(self) -> None:
        """Test deleting webhook subscription successfully."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        input_data = DeleteWebhookSubscriptionInput(
            organization_id=organization.id, subscription_id=123
        )

        mock_service = Mock()
        mock_service.delete_webhook_subscription.return_value = True
        mock_deps = WebhookMutationDependencies(calendar_service=mock_service)

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookDeleteResult,
                cast(Callable[..., Any], mutations.delete_webhook_subscription)(input_data),
            )

            assert result.success is True
            assert result.error_message is None
            mock_service.delete_webhook_subscription.assert_called_once_with(subscription_id=123)

    def test_delete_webhook_subscription_not_found(self) -> None:
        """Test deleting non-existent webhook subscription."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        input_data = DeleteWebhookSubscriptionInput(
            organization_id=organization.id, subscription_id=123
        )

        mock_service = Mock()
        mock_service.delete_webhook_subscription.return_value = False
        mock_deps = WebhookMutationDependencies(calendar_service=mock_service)

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookDeleteResult,
                cast(Callable[..., Any], mutations.delete_webhook_subscription)(input_data),
            )
            assert result.error_message == "Subscription not found"

    def test_delete_webhook_subscription_service_error(self) -> None:
        """Test deleting webhook subscription with service error."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        input_data = DeleteWebhookSubscriptionInput(
            organization_id=organization.id, subscription_id=123
        )

        mock_service = Mock()
        mock_service.delete_webhook_subscription.side_effect = ValueError("Service error")
        mock_deps = WebhookMutationDependencies(calendar_service=mock_service)

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookDeleteResult,
                cast(Callable[..., Any], mutations.delete_webhook_subscription)(input_data),
            )

            assert result.success is False
            assert result.error_message is not None
            assert "Failed to delete subscription: Service error" in result.error_message

    def test_refresh_webhook_subscription_success(self) -> None:
        """Test refreshing webhook subscription successfully."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        input_data = RefreshWebhookSubscriptionInput(
            organization_id=organization.id, subscription_id=123
        )

        mock_subscription = Mock()
        mock_service = Mock()
        mock_service.refresh_webhook_subscription.return_value = mock_subscription
        mock_deps = WebhookMutationDependencies(calendar_service=mock_service)

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            result = cast(
                WebhookSubscriptionResult,
                cast(Callable[..., Any], mutations.refresh_webhook_subscription)(input_data),
            )

            assert result.success is True
            assert result.subscription == mock_subscription
            assert result.error_message is None
            mock_service.refresh_webhook_subscription.assert_called_once_with(subscription_id=123)

    def test_refresh_webhook_subscription_not_found(self) -> None:
        """Test refreshing non-existent webhook subscription."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        input_data = RefreshWebhookSubscriptionInput(
            organization_id=organization.id, subscription_id=123
        )

        mock_service = Mock()
        mock_service.refresh_webhook_subscription.return_value = None
        mock_deps = WebhookMutationDependencies(calendar_service=mock_service)

        with patch(
            "calendar_integration.mutations.get_webhook_mutation_dependencies",
            return_value=mock_deps,
        ):
            cast(
                WebhookSubscriptionResult,
                cast(Callable[..., Any], mutations.refresh_webhook_subscription)(input_data),
            )

    def test_cleanup_webhook_events_success(self) -> None:
        """Test cleaning up webhook events successfully."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        input_data = CleanupWebhookEventsInput(organization_id=organization.id, days_to_keep=30)

        with patch.object(WebhookAnalyticsService, "cleanup_old_webhook_events", return_value=5):
            result = cast(
                WebhookCleanupResult,
                cast(Callable[..., Any], mutations.cleanup_webhook_events)(input_data),
            )

            assert result.success is True
            assert result.deleted_count == 5
            assert result.error_message is None

    def test_cleanup_webhook_events_organization_not_found(self) -> None:
        """Test cleaning up webhook events with non-existent organization."""
        mutations = CalendarWebhookMutations()

        # Use a non-existent organization ID
        input_data = CleanupWebhookEventsInput(organization_id=99999, days_to_keep=30)

        result = cast(
            WebhookCleanupResult,
            cast(Callable[..., Any], mutations.cleanup_webhook_events)(input_data),
        )

        assert result.success is False
        result = cast(
            WebhookCleanupResult,
            cast(Callable[..., Any], mutations.cleanup_webhook_events)(input_data),
        )
        """Test cleaning up webhook events with service error."""
        mutations = CalendarWebhookMutations()
        organization = baker.make(Organization)

        input_data = CleanupWebhookEventsInput(organization_id=organization.id, days_to_keep=30)

        with patch.object(
            WebhookAnalyticsService,
            "cleanup_old_webhook_events",
            side_effect=ValueError("Service error"),
        ):
            result = cast(
                WebhookCleanupResult,
                cast(Callable[..., Any], mutations.cleanup_webhook_events)(input_data),
            )

            assert result.success is False
            assert result.deleted_count == 0
            assert result.error_message is not None
            assert "Failed to cleanup events: Service error" in result.error_message
