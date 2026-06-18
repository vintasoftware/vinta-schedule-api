"""Unit tests for webhooks GraphQL types."""

import pytest
import strawberry
import strawberry_django
from model_bakery import baker
from strawberry_django.optimizer import DjangoOptimizerExtension

from organizations.models import Organization
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.graphql import WebhookConfigurationGraphQLType, WebhookEventGraphQLType
from webhooks.models import WebhookConfiguration, WebhookEvent


@strawberry.type
class _TestQuery:
    """Minimal schema query used only in this test module.

    Requires ``organization_id`` to satisfy the multi-tenancy manager contract.
    """

    @strawberry_django.field
    def webhook_configuration(
        self,
        organization_id: int,
        pk: int,
    ) -> WebhookConfigurationGraphQLType | None:
        return (  # type: ignore[return-value]
            WebhookConfiguration.objects.filter_by_organization(organization_id)
            .filter(pk=pk)
            .first()
        )

    @strawberry_django.field
    def webhook_event(
        self,
        organization_id: int,
        pk: int,
    ) -> WebhookEventGraphQLType | None:
        return (  # type: ignore[return-value]
            WebhookEvent.objects.filter_by_organization(organization_id).filter(pk=pk).first()
        )


_test_schema = strawberry.Schema(
    query=_TestQuery,
    extensions=[DjangoOptimizerExtension],
)


@pytest.fixture
def organization(db):
    return baker.make(Organization, name="Test Org")


@pytest.fixture
def webhook_configuration(organization):
    return baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/webhook",
        headers={"Authorization": "Bearer token"},
    )


@pytest.fixture
def webhook_event(organization, webhook_configuration):
    return baker.make(
        WebhookEvent,
        organization=organization,
        configuration=webhook_configuration,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/webhook",
        status=WebhookStatus.SUCCESS,
        payload={"key": "value"},
        response_status=200,
        retry_number=0,
    )


@pytest.mark.django_db
class TestWebhookConfigurationGraphQLType:
    """Tests for WebhookConfigurationGraphQLType field resolution via a test schema."""

    _CONFIG_FRAGMENT = "{ id eventType url headers created modified }"

    def _execute(self, organization, webhook_configuration, fields: str = "") -> dict:
        if not fields:
            fields = self._CONFIG_FRAGMENT
        query = f"query($orgId: Int!, $pk: Int!) {{ webhookConfiguration(organizationId: $orgId, pk: $pk) {fields} }}"
        result = _test_schema.execute_sync(
            query,
            variable_values={
                "orgId": organization.pk,
                "pk": webhook_configuration.pk,
            },
        )
        assert result.errors is None, result.errors
        return (result.data or {})["webhookConfiguration"]

    def test_id_field(self, organization, webhook_configuration):
        """Type resolves the id field."""
        data = self._execute(organization, webhook_configuration, "{ id }")
        assert data["id"] == str(webhook_configuration.pk)

    def test_event_type_field(self, organization, webhook_configuration):
        """Type resolves event_type field."""
        data = self._execute(organization, webhook_configuration, "{ eventType }")
        assert data["eventType"] == WebhookEventType.CALENDAR_EVENT_CREATED

    def test_url_field(self, organization, webhook_configuration):
        """Type resolves url field."""
        data = self._execute(organization, webhook_configuration, "{ url }")
        assert data["url"] == "https://example.com/webhook"

    def test_headers_field(self, organization, webhook_configuration):
        """Type resolves headers JSON field with correct dict value."""
        data = self._execute(organization, webhook_configuration, "{ headers }")
        assert data["headers"] == {"Authorization": "Bearer token"}

    def test_created_and_modified_fields(self, organization, webhook_configuration):
        """Type resolves created and modified timestamp fields."""
        data = self._execute(organization, webhook_configuration, "{ created modified }")
        assert data["created"] is not None
        assert data["modified"] is not None

    def test_full_fragment_resolves(self, organization, webhook_configuration):
        """All declared fields resolve without errors."""
        data = self._execute(organization, webhook_configuration)
        assert data is not None


@pytest.mark.django_db
class TestWebhookEventGraphQLType:
    """Tests for WebhookEventGraphQLType field resolution via a test schema."""

    _EVENT_FRAGMENT = (
        "{ id eventType url status responseStatus retryNumber configurationId created modified }"
    )

    def _execute(self, organization, webhook_event, fields: str = "") -> dict:
        if not fields:
            fields = self._EVENT_FRAGMENT
        query = f"query($orgId: Int!, $pk: Int!) {{ webhookEvent(organizationId: $orgId, pk: $pk) {fields} }}"
        result = _test_schema.execute_sync(
            query,
            variable_values={
                "orgId": organization.pk,
                "pk": webhook_event.pk,
            },
        )
        assert result.errors is None, result.errors
        return (result.data or {})["webhookEvent"]

    def test_id_field(self, organization, webhook_event):
        """Type resolves the id field."""
        data = self._execute(organization, webhook_event, "{ id }")
        assert data["id"] == str(webhook_event.pk)

    def test_event_type_field(self, organization, webhook_event):
        """Type resolves event_type field."""
        data = self._execute(organization, webhook_event, "{ eventType }")
        assert data["eventType"] == WebhookEventType.CALENDAR_EVENT_CREATED

    def test_url_field(self, organization, webhook_event):
        """Type resolves url field."""
        data = self._execute(organization, webhook_event, "{ url }")
        assert data["url"] == "https://example.com/webhook"

    def test_status_field(self, organization, webhook_event):
        """Type resolves status field."""
        data = self._execute(organization, webhook_event, "{ status }")
        assert data["status"] == WebhookStatus.SUCCESS

    def test_response_status_field(self, organization, webhook_event):
        """Type resolves response_status field."""
        data = self._execute(organization, webhook_event, "{ responseStatus }")
        assert data["responseStatus"] == 200

    def test_retry_number_field(self, organization, webhook_event):
        """Type resolves retry_number field."""
        data = self._execute(organization, webhook_event, "{ retryNumber }")
        assert data["retryNumber"] == 0

    def test_configuration_id_field(self, organization, webhook_event, webhook_configuration):
        """Type resolves configuration_id custom field via concrete FK."""
        data = self._execute(organization, webhook_event, "{ configurationId }")
        assert data["configurationId"] == webhook_configuration.pk

    def test_created_and_modified_fields(self, organization, webhook_event):
        """Type resolves created and modified timestamp fields."""
        data = self._execute(organization, webhook_event, "{ created modified }")
        assert data["created"] is not None
        assert data["modified"] is not None

    def test_full_fragment_resolves(self, organization, webhook_event):
        """All declared fields resolve without errors."""
        data = self._execute(organization, webhook_event)
        assert data is not None
