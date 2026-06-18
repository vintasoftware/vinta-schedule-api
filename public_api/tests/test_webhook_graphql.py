"""Integration tests for WebhookConfiguration CRUD and WebhookEvent history over the public GraphQL API (Phases 6 & 7)."""

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess
from public_api.services import PublicAPIAuthService
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.models import WebhookConfiguration, WebhookEvent


# ---------------------------------------------------------------------------
# GraphQL document strings
# ---------------------------------------------------------------------------

LIST_WEBHOOK_CONFIGURATIONS_QUERY = """
query WebhookConfigurations($offset: Int, $limit: Int) {
    webhookConfigurations(offset: $offset, limit: $limit) {
        id
        eventType
        url
        headers
    }
}
"""

LIST_WEBHOOK_DELIVERY_EVENTS_QUERY = """
query WebhookDeliveryEvents($offset: Int, $limit: Int) {
    webhookDeliveryEvents(offset: $offset, limit: $limit) {
        id
        eventType
        url
        status
        responseStatus
        retryNumber
        configurationId
    }
}
"""

CREATE_WEBHOOK_CONFIGURATION_MUTATION = """
mutation CreateWebhookConfiguration($input: CreateWebhookConfigurationInput!) {
    createWebhookConfiguration(input: $input) {
        configuration {
            id
            eventType
            url
            headers
        }
        errorMessage
    }
}
"""

UPDATE_WEBHOOK_CONFIGURATION_MUTATION = """
mutation UpdateWebhookConfiguration($input: UpdateWebhookConfigurationInput!) {
    updateWebhookConfiguration(input: $input) {
        configuration {
            id
            eventType
            url
            headers
        }
        errorMessage
    }
}
"""

DELETE_WEBHOOK_CONFIGURATION_MUTATION = """
mutation DeleteWebhookConfiguration($input: DeleteWebhookConfigurationInput!) {
    deleteWebhookConfiguration(input: $input) {
        success
        errorMessage
    }
}
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization():
    """A plain organization for tests that don't need reseller capabilities."""
    return baker.make(Organization, name="Test Org")


@pytest.fixture
def other_organization():
    """A second organization used for tenant-isolation assertions."""
    return baker.make(Organization, name="Other Org")


def _make_system_user(organization, resources=None):
    """Create a SystemUser with a plaintext token and the given resource scopes."""
    if resources is None:
        resources = [PublicAPIResources.WEBHOOK_CONFIGURATION]
    auth_service = PublicAPIAuthService()
    system_user, token = auth_service.create_system_user(
        integration_name=f"test_integration_{organization.id}",
        organization=organization,
    )
    for resource in resources:
        baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
    return system_user, token, auth_service


@pytest.fixture
def system_user_with_webhook_access(organization):
    """SystemUser for `organization` with WEBHOOK_CONFIGURATION scope."""
    return _make_system_user(organization)


@pytest.fixture
def other_system_user(other_organization):
    """SystemUser for `other_organization` with WEBHOOK_CONFIGURATION scope."""
    return _make_system_user(other_organization)


@pytest.fixture
def webhook_configuration(organization):
    """A live (non-deleted) WebhookConfiguration in `organization`."""
    return baker.make(
        WebhookConfiguration,
        organization=organization,
        event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
        url="https://example.com/webhook",
        headers={"Authorization": "Bearer secret"},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(client, system_user, token, query, variables=None):
    """Post a GraphQL request authenticated as `system_user`."""
    from di_core.containers import container

    auth_service = PublicAPIAuthService()
    with container.public_api_auth_service.override(auth_service):
        return client.post(
            "/graphql/",
            data={"query": query, "variables": variables or {}},
            format="json",
            headers={"authorization": f"Bearer {system_user.id}:{token}"},
        )


# ---------------------------------------------------------------------------
# Tests: webhookConfigurations query
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWebhookConfigurationsQuery:
    """Tests for the webhookConfigurations list query."""

    def setup_method(self):
        self.client = APIClient()

    def test_list_returns_own_org_configs(self, organization, system_user_with_webhook_access):
        """Query returns all non-deleted configurations for the caller's org."""
        system_user, token, _ = system_user_with_webhook_access

        # Create two configs in the org
        cfg1 = baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.CALENDAR_EVENT_CREATED,
            url="https://example.com/hook1",
            deleted_at=None,
        )
        cfg2 = baker.make(
            WebhookConfiguration,
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
            url="https://example.com/hook2",
            deleted_at=None,
        )

        response = _post(self.client, system_user, token, LIST_WEBHOOK_CONFIGURATIONS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data.get("errors")

        ids_returned = {int(c["id"]) for c in data["data"]["webhookConfigurations"]}
        assert cfg1.id in ids_returned
        assert cfg2.id in ids_returned

    def test_list_excludes_deleted_configs(self, organization, system_user_with_webhook_access):
        """Soft-deleted configs are excluded from the list."""
        import datetime

        system_user, token, _ = system_user_with_webhook_access

        live = baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/live",
            deleted_at=None,
        )
        baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/deleted",
            deleted_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        )

        response = _post(self.client, system_user, token, LIST_WEBHOOK_CONFIGURATIONS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors")

        ids_returned = {int(c["id"]) for c in data["data"]["webhookConfigurations"]}
        assert live.id in ids_returned
        assert len(ids_returned) == 1

    def test_list_excludes_other_org_configs(
        self,
        organization,
        other_organization,
        system_user_with_webhook_access,
    ):
        """Tenant isolation: configs from another org are never returned."""
        system_user, token, _ = system_user_with_webhook_access

        own = baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/own",
            deleted_at=None,
        )
        baker.make(
            WebhookConfiguration,
            organization=other_organization,
            url="https://example.com/other",
            deleted_at=None,
        )

        response = _post(self.client, system_user, token, LIST_WEBHOOK_CONFIGURATIONS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors")

        ids_returned = {int(c["id"]) for c in data["data"]["webhookConfigurations"]}
        assert own.id in ids_returned
        assert len(ids_returned) == 1

    def test_list_denied_without_webhook_configuration_scope(self, organization):
        """Token without WEBHOOK_CONFIGURATION scope is denied."""
        # System user with a different scope (calendar)
        system_user, token, _ = _make_system_user(
            organization, resources=[PublicAPIResources.CALENDAR]
        )

        response = _post(self.client, system_user, token, LIST_WEBHOOK_CONFIGURATIONS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_list_denied_for_unauthenticated_request(self):
        """Unauthenticated request is denied."""
        from di_core.containers import container

        auth_service = PublicAPIAuthService()
        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={"query": LIST_WEBHOOK_CONFIGURATIONS_QUERY, "variables": {}},
                format="json",
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0


# ---------------------------------------------------------------------------
# Tests: createWebhookConfiguration mutation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCreateWebhookConfigurationMutation:
    """Tests for the createWebhookConfiguration mutation."""

    def setup_method(self):
        self.client = APIClient()

    def test_create_stores_and_returns_config(self, organization, system_user_with_webhook_access):
        """Happy path: config is created in DB and returned with correct fields."""
        system_user, token, _ = system_user_with_webhook_access

        variables = {
            "input": {
                "eventType": WebhookEventType.CALENDAR_EVENT_CREATED,
                "url": "https://example.com/hook",
                "headers": {"X-Secret": "abc"},
            }
        }

        response = _post(
            self.client,
            system_user,
            token,
            CREATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data.get("errors")

        result = data["data"]["createWebhookConfiguration"]
        assert result["errorMessage"] is None
        cfg = result["configuration"]
        assert cfg is not None
        assert cfg["eventType"] == WebhookEventType.CALENDAR_EVENT_CREATED
        assert cfg["url"] == "https://example.com/hook"
        assert cfg["headers"] == {"X-Secret": "abc"}

        # Verify the row exists in DB
        db_cfg = WebhookConfiguration.objects.filter_by_organization(organization.id).get(
            id=int(cfg["id"])
        )
        assert db_cfg.organization == organization
        assert db_cfg.deleted_at is None

    def test_create_is_org_scoped(
        self, organization, other_organization, system_user_with_webhook_access
    ):
        """Created config belongs to acting org, not any other org."""
        system_user, token, _ = system_user_with_webhook_access

        variables = {
            "input": {
                "eventType": WebhookEventType.CALENDAR_EVENT_CREATED,
                "url": "https://example.com/hook",
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            CREATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        cfg_id = int(data["data"]["createWebhookConfiguration"]["configuration"]["id"])

        # Must belong to `organization`, not `other_organization`
        assert (
            WebhookConfiguration.objects.filter_by_organization(organization.id)
            .filter(id=cfg_id)
            .exists()
        )
        assert (
            not WebhookConfiguration.objects.filter_by_organization(other_organization.id)
            .filter(id=cfg_id)
            .exists()
        )

    def test_create_invalid_event_type_raises_error(
        self, organization, system_user_with_webhook_access
    ):
        """Supplying an unknown event_type returns a GraphQL error."""
        system_user, token, _ = system_user_with_webhook_access

        variables = {
            "input": {
                "eventType": "not_a_real_event_type",
                "url": "https://example.com/hook",
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            CREATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "invalid event_type" in str(data["errors"]).lower()

    def test_create_invalid_url_raises_error(self, organization, system_user_with_webhook_access):
        """Supplying an invalid url returns a GraphQL error."""
        system_user, token, _ = system_user_with_webhook_access

        variables = {
            "input": {
                "eventType": WebhookEventType.CALENDAR_EVENT_CREATED,
                "url": "not-a-url",
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            CREATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "invalid url" in str(data["errors"]).lower()

    def test_create_denied_without_webhook_configuration_scope(self, organization):
        """Token without WEBHOOK_CONFIGURATION scope is denied."""
        system_user, token, _ = _make_system_user(
            organization, resources=[PublicAPIResources.CALENDAR]
        )

        variables = {
            "input": {
                "eventType": WebhookEventType.CALENDAR_EVENT_CREATED,
                "url": "https://example.com/hook",
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            CREATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()


# ---------------------------------------------------------------------------
# Tests: updateWebhookConfiguration mutation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestUpdateWebhookConfigurationMutation:
    """Tests for the updateWebhookConfiguration mutation."""

    def setup_method(self):
        self.client = APIClient()

    def test_update_changes_fields(
        self, organization, webhook_configuration, system_user_with_webhook_access
    ):
        """Happy path: fields are updated and returned."""
        system_user, token, _ = system_user_with_webhook_access

        new_url = "https://example.com/updated"
        variables = {
            "input": {
                "id": webhook_configuration.id,
                "url": new_url,
                "eventType": WebhookEventType.ORGANIZATION_MEMBER_CREATED,
                "headers": {"X-New": "header"},
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            UPDATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data.get("errors")

        result = data["data"]["updateWebhookConfiguration"]
        assert result["errorMessage"] is None
        cfg = result["configuration"]
        assert cfg["url"] == new_url
        assert cfg["eventType"] == WebhookEventType.ORGANIZATION_MEMBER_CREATED
        assert cfg["headers"] == {"X-New": "header"}

        # Verify DB row
        webhook_configuration.refresh_from_db()
        assert webhook_configuration.url == new_url

    def test_update_partial_only_url(
        self, organization, webhook_configuration, system_user_with_webhook_access
    ):
        """Partial update: only url provided; event_type and headers stay unchanged."""
        system_user, token, _ = system_user_with_webhook_access
        original_event_type = webhook_configuration.event_type
        original_headers = dict(webhook_configuration.headers)

        variables = {
            "input": {
                "id": webhook_configuration.id,
                "url": "https://example.com/partial",
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            UPDATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors")

        webhook_configuration.refresh_from_db()
        assert webhook_configuration.url == "https://example.com/partial"
        assert webhook_configuration.event_type == original_event_type
        assert webhook_configuration.headers == original_headers

    def test_update_cross_org_returns_not_found(
        self,
        other_organization,
        other_system_user,
        webhook_configuration,
    ):
        """Tenant isolation: org B cannot update org A's config — returns not-found error."""
        system_user, token, _ = other_system_user

        variables = {
            "input": {
                "id": webhook_configuration.id,  # belongs to `organization`, not `other_organization`
                "url": "https://evil.com/hook",
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            UPDATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data.get("errors")

        result = data["data"]["updateWebhookConfiguration"]
        assert result["configuration"] is None
        assert "not found" in (result["errorMessage"] or "").lower()

        # Row must be unchanged
        webhook_configuration.refresh_from_db()
        assert webhook_configuration.url == "https://example.com/webhook"

    def test_update_deleted_config_returns_not_found(
        self, organization, system_user_with_webhook_access
    ):
        """Updating a soft-deleted config returns a not-found error."""
        import datetime

        system_user, token, _ = system_user_with_webhook_access
        deleted_cfg = baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/deleted",
            deleted_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        )

        variables = {"input": {"id": deleted_cfg.id, "url": "https://example.com/updated"}}
        response = _post(
            self.client,
            system_user,
            token,
            UPDATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" not in data or not data.get("errors")
        result = data["data"]["updateWebhookConfiguration"]
        assert result["configuration"] is None
        assert "not found" in (result["errorMessage"] or "").lower()

    def test_update_invalid_event_type_raises_error(
        self, organization, webhook_configuration, system_user_with_webhook_access
    ):
        """Invalid event_type in update raises a GraphQL error."""
        system_user, token, _ = system_user_with_webhook_access

        variables = {
            "input": {
                "id": webhook_configuration.id,
                "eventType": "bad_event_type",
            }
        }
        response = _post(
            self.client,
            system_user,
            token,
            UPDATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "invalid event_type" in str(data["errors"]).lower()

    def test_update_denied_without_webhook_configuration_scope(
        self, organization, webhook_configuration
    ):
        """Token without WEBHOOK_CONFIGURATION scope is denied."""
        system_user, token, _ = _make_system_user(
            organization, resources=[PublicAPIResources.CALENDAR]
        )

        variables = {"input": {"id": webhook_configuration.id, "url": "https://example.com/hook"}}
        response = _post(
            self.client,
            system_user,
            token,
            UPDATE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()


# ---------------------------------------------------------------------------
# Tests: deleteWebhookConfiguration mutation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeleteWebhookConfigurationMutation:
    """Tests for the deleteWebhookConfiguration mutation."""

    def setup_method(self):
        self.client = APIClient()

    def test_delete_soft_deletes_config(
        self, organization, webhook_configuration, system_user_with_webhook_access
    ):
        """Happy path: config is soft-deleted (deleted_at set), not removed from DB."""
        system_user, token, _ = system_user_with_webhook_access

        variables = {"input": {"id": webhook_configuration.id}}
        response = _post(
            self.client,
            system_user,
            token,
            DELETE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data.get("errors")

        result = data["data"]["deleteWebhookConfiguration"]
        assert result["success"] is True
        assert result["errorMessage"] is None

        # Row still exists in DB but with deleted_at set
        webhook_configuration.refresh_from_db()
        assert webhook_configuration.deleted_at is not None

    def test_delete_cross_org_returns_not_found(
        self,
        other_organization,
        other_system_user,
        webhook_configuration,
    ):
        """Tenant isolation: org B cannot delete org A's config."""
        system_user, token, _ = other_system_user

        variables = {"input": {"id": webhook_configuration.id}}
        response = _post(
            self.client,
            system_user,
            token,
            DELETE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data.get("errors")

        result = data["data"]["deleteWebhookConfiguration"]
        assert result["success"] is False
        assert "not found" in (result["errorMessage"] or "").lower()

        # Row must be unchanged (not deleted)
        webhook_configuration.refresh_from_db()
        assert webhook_configuration.deleted_at is None

    def test_delete_already_deleted_config_returns_not_found(
        self, organization, system_user_with_webhook_access
    ):
        """Deleting an already-deleted config returns a not-found error."""
        import datetime

        system_user, token, _ = system_user_with_webhook_access
        deleted_cfg = baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/gone",
            deleted_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC),
        )

        variables = {"input": {"id": deleted_cfg.id}}
        response = _post(
            self.client,
            system_user,
            token,
            DELETE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" not in data or not data.get("errors")
        result = data["data"]["deleteWebhookConfiguration"]
        assert result["success"] is False
        assert "not found" in (result["errorMessage"] or "").lower()

    def test_delete_config_disappears_from_list(
        self, organization, webhook_configuration, system_user_with_webhook_access
    ):
        """After deletion, config no longer appears in webhookConfigurations list."""
        system_user, token, _ = system_user_with_webhook_access

        # Confirm it appears in the list first
        list_response = _post(self.client, system_user, token, LIST_WEBHOOK_CONFIGURATIONS_QUERY)
        ids_before = {int(c["id"]) for c in list_response.json()["data"]["webhookConfigurations"]}
        assert webhook_configuration.id in ids_before

        # Delete it
        _post(
            self.client,
            system_user,
            token,
            DELETE_WEBHOOK_CONFIGURATION_MUTATION,
            {"input": {"id": webhook_configuration.id}},
        )

        # Should no longer appear in list
        list_response = _post(self.client, system_user, token, LIST_WEBHOOK_CONFIGURATIONS_QUERY)
        ids_after = {int(c["id"]) for c in list_response.json()["data"]["webhookConfigurations"]}
        assert webhook_configuration.id not in ids_after

    def test_delete_denied_without_webhook_configuration_scope(
        self, organization, webhook_configuration
    ):
        """Token without WEBHOOK_CONFIGURATION scope is denied."""
        system_user, token, _ = _make_system_user(
            organization, resources=[PublicAPIResources.CALENDAR]
        )

        variables = {"input": {"id": webhook_configuration.id}}
        response = _post(
            self.client,
            system_user,
            token,
            DELETE_WEBHOOK_CONFIGURATION_MUTATION,
            variables,
        )

        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

        # Row must be unchanged
        webhook_configuration.refresh_from_db()
        assert webhook_configuration.deleted_at is None


# ---------------------------------------------------------------------------
# Tests: webhookDeliveryEvents query (Phase 7)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWebhookDeliveryEventsQuery:
    """Tests for the read-only webhookDeliveryEvents list query (delivery history)."""

    def setup_method(self):
        self.client = APIClient()

    def _make_event(self, organization, configuration, **kwargs):
        """Create a WebhookEvent for the given organization and configuration."""
        defaults = {
            "organization": organization,
            "configuration": configuration,
            "event_type": WebhookEventType.CALENDAR_EVENT_CREATED,
            "url": configuration.url,
            "status": WebhookStatus.SUCCESS,
            "payload": {"key": "value"},
        }
        defaults.update(kwargs)
        return baker.make(WebhookEvent, **defaults)

    def test_list_returns_own_org_events(self, organization, system_user_with_webhook_access):
        """webhookDeliveryEvents returns all events for the caller's organization."""
        system_user, token, _ = system_user_with_webhook_access
        cfg = baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/hook",
        )
        event1 = self._make_event(organization, cfg)
        event2 = self._make_event(
            organization, cfg, event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED
        )

        response = _post(self.client, system_user, token, LIST_WEBHOOK_DELIVERY_EVENTS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors"), data.get("errors")

        events_returned = data["data"]["webhookDeliveryEvents"]
        ids_returned = {int(e["id"]) for e in events_returned}
        assert event1.id in ids_returned
        assert event2.id in ids_returned

        # Verify the configuration_id custom resolver reads configuration_fk_id correctly.
        event1_data = next(e for e in events_returned if int(e["id"]) == event1.id)
        assert int(event1_data["configurationId"]) == cfg.id

    def test_list_excludes_other_org_events(
        self,
        organization,
        other_organization,
        system_user_with_webhook_access,
    ):
        """Tenant isolation: events from another org are never returned."""
        system_user, token, _ = system_user_with_webhook_access

        own_cfg = baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/own",
        )
        other_cfg = baker.make(
            WebhookConfiguration,
            organization=other_organization,
            url="https://example.com/other",
        )
        own_event = self._make_event(organization, own_cfg)
        other_event = self._make_event(other_organization, other_cfg)

        response = _post(self.client, system_user, token, LIST_WEBHOOK_DELIVERY_EVENTS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors")

        ids_returned = {int(e["id"]) for e in data["data"]["webhookDeliveryEvents"]}
        assert own_event.id in ids_returned
        assert other_event.id not in ids_returned

    def test_list_returns_newest_first(self, organization, system_user_with_webhook_access):
        """webhookDeliveryEvents returns events ordered newest first (descending pk)."""
        system_user, token, _ = system_user_with_webhook_access
        cfg = baker.make(
            WebhookConfiguration,
            organization=organization,
            url="https://example.com/hook",
        )
        event_first = self._make_event(organization, cfg)
        event_second = self._make_event(organization, cfg)

        response = _post(self.client, system_user, token, LIST_WEBHOOK_DELIVERY_EVENTS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" not in data or not data.get("errors")

        ids_returned = [int(e["id"]) for e in data["data"]["webhookDeliveryEvents"]]
        # Newest (highest pk) should come first
        assert ids_returned.index(event_second.id) < ids_returned.index(event_first.id)

    def test_list_denied_without_webhook_configuration_scope(self, organization):
        """Token without WEBHOOK_CONFIGURATION scope is denied for webhookDeliveryEvents."""
        system_user, token, _ = _make_system_user(
            organization, resources=[PublicAPIResources.CALENDAR]
        )

        response = _post(self.client, system_user, token, LIST_WEBHOOK_DELIVERY_EVENTS_QUERY)
        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0
        assert "don't have access" in str(data["errors"]).lower()

    def test_list_denied_for_unauthenticated_request(self):
        """Unauthenticated request is denied for webhookDeliveryEvents."""
        from di_core.containers import container

        auth_service = PublicAPIAuthService()
        with container.public_api_auth_service.override(auth_service):
            response = self.client.post(
                "/graphql/",
                data={"query": LIST_WEBHOOK_DELIVERY_EVENTS_QUERY, "variables": {}},
                format="json",
            )

        assert response.status_code == 200
        data = response.json()
        assert "errors" in data and len(data["errors"]) > 0

    def test_no_write_path_for_events(self, organization, system_user_with_webhook_access):
        """Verify no mutation exists for creating or mutating webhook events."""
        system_user, token, _ = system_user_with_webhook_access

        # Attempt to call a non-existent createWebhookEvent mutation
        create_mutation = """
        mutation {
            createWebhookEvent(input: {eventType: "calendar_event_created", url: "https://example.com"}) {
                id
            }
        }
        """
        response = _post(self.client, system_user, token, create_mutation)
        assert response.status_code == 200
        data = response.json()
        # Must error — mutation doesn't exist in schema, and the error must
        # specifically name the missing field so this is a genuine no-write-path proof.
        assert "errors" in data and len(data["errors"]) > 0
        error_messages = " ".join(str(e.get("message", "")) for e in data["errors"])
        assert "Cannot query field" in error_messages and "createWebhookEvent" in error_messages
