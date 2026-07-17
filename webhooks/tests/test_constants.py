import pytest

from webhooks.constants import WEBHOOK_EVENT_DESCRIPTIONS, WebhookEventType


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


@pytest.mark.django_db
class TestWebhookEventDescriptions:
    """Test suite locking in the webhook event catalog's description mapping.

    This is the important check here: it iterates the enum itself (never a hardcoded
    list of members) so a new ``WebhookEventType`` member cannot ship without a
    description.
    """

    def test_every_member_has_a_non_empty_description(self):
        """Every WebhookEventType member must have a non-empty description.

        Iterating the enum (not a hardcoded list) means adding an eighth member
        without a matching WEBHOOK_EVENT_DESCRIPTIONS entry fails this test.
        """
        for member in WebhookEventType:
            description = WEBHOOK_EVENT_DESCRIPTIONS.get(member)
            assert description, f"{member} is missing a WEBHOOK_EVENT_DESCRIPTIONS entry"
            assert description.strip(), f"{member} has a blank description"

    def test_no_orphan_description_keys(self):
        """WEBHOOK_EVENT_DESCRIPTIONS must have no keys outside WebhookEventType members.

        Prevents a rename from leaving a stale, orphaned entry behind.
        """
        member_values = set(WebhookEventType)
        assert set(WEBHOOK_EVENT_DESCRIPTIONS.keys()) <= member_values
