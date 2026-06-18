import pytest

from webhooks.constants import WebhookEventType


@pytest.mark.django_db
class TestWebhookEventType:
    """Test suite for WebhookEventType constants."""

    def test_organization_member_created_constant_exists(self):
        """Test that ORGANIZATION_MEMBER_CREATED constant is defined."""
        assert hasattr(WebhookEventType, "ORGANIZATION_MEMBER_CREATED")

    def test_organization_member_created_value(self):
        """Test that ORGANIZATION_MEMBER_CREATED has correct value."""
        assert WebhookEventType.ORGANIZATION_MEMBER_CREATED == "organization_member_created"

    def test_organization_member_created_in_choices(self):
        """Test that ORGANIZATION_MEMBER_CREATED is in the choices list."""
        choices = [choice[0] for choice in WebhookEventType.choices]
        assert "organization_member_created" in choices

    def test_organization_member_created_display_name(self):
        """Test that ORGANIZATION_MEMBER_CREATED has correct display name."""
        display_name = WebhookEventType("organization_member_created").label
        assert display_name == "Organization member created"
