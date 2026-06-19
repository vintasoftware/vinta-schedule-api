from unittest.mock import MagicMock

import pytest
from model_bakery import baker

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from users.models import User
from webhooks.constants import WebhookEventType
from webhooks.services.webhook_membership_side_effects import WebhookMembershipSideEffectsService


@pytest.mark.django_db
class TestWebhookMembershipSideEffectsService:
    """Unit tests for WebhookMembershipSideEffectsService.on_member_created."""

    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="Test Org")

    @pytest.fixture
    def user(self):
        return baker.make(User, email="member@example.com")

    @pytest.fixture
    def mock_webhook_service(self):
        mock = MagicMock()
        mock.send_event.return_value = []
        return mock

    @pytest.fixture
    def service(self, mock_webhook_service):
        """Create WebhookMembershipSideEffectsService with the webhook_service mocked."""
        return WebhookMembershipSideEffectsService(webhook_service=mock_webhook_service)

    def test_on_member_created_active_membership_emits_event(
        self, service, mock_webhook_service, organization, user, django_capture_on_commit_callbacks
    ):
        """on_member_created emits ORGANIZATION_MEMBER_CREATED for an active membership."""
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.on_member_created(membership)

        mock_webhook_service.send_event.assert_called_once_with(
            organization=organization,
            event_type=WebhookEventType.ORGANIZATION_MEMBER_CREATED,
            payload={
                "user_id": user.id,
                "email": user.email,
                "organization_id": organization.id,
                "organization_name": organization.name,
                "membership_role": OrganizationRole.MEMBER,
                "membership_id": membership.id,
            },
        )

    def test_on_member_created_active_admin_membership_emits_event(
        self, service, mock_webhook_service, organization, user, django_capture_on_commit_callbacks
    ):
        """on_member_created emits with correct admin role for admin memberships."""
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.on_member_created(membership)

        mock_webhook_service.send_event.assert_called_once()
        call_kwargs = mock_webhook_service.send_event.call_args[1]
        assert call_kwargs["payload"]["membership_role"] == OrganizationRole.ADMIN
        assert call_kwargs["event_type"] == WebhookEventType.ORGANIZATION_MEMBER_CREATED

    def test_on_member_created_inactive_membership_does_not_emit(
        self, service, mock_webhook_service, organization, user, django_capture_on_commit_callbacks
    ):
        """on_member_created must NOT emit for inactive memberships (is_active=False)."""
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.on_member_created(membership)

        mock_webhook_service.send_event.assert_not_called()

    def test_on_member_created_payload_fields_match_contract(
        self, service, mock_webhook_service, organization, django_capture_on_commit_callbacks
    ):
        """Payload contains exactly the fields mandated by OrganizationMemberCreatedWebhookPayload."""
        user = baker.make(User, email="payload@example.com")
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        with django_capture_on_commit_callbacks(execute=True):
            service.on_member_created(membership)

        call_kwargs = mock_webhook_service.send_event.call_args[1]
        payload = call_kwargs["payload"]

        assert payload["user_id"] == user.id
        assert payload["email"] == user.email
        assert payload["organization_id"] == organization.id
        assert payload["organization_name"] == organization.name
        assert payload["membership_role"] == OrganizationRole.MEMBER
        assert payload["membership_id"] == membership.id
        # Ensure no extra fields sneak in
        assert set(payload.keys()) == {
            "user_id",
            "email",
            "organization_id",
            "organization_name",
            "membership_role",
            "membership_id",
        }

    def test_on_member_created_organization_scoped_to_membership_org(
        self, service, mock_webhook_service, user, django_capture_on_commit_callbacks
    ):
        """send_event is called with the membership's organization, not any other org."""
        org_a = baker.make(Organization, name="Org A")
        org_b = baker.make(Organization, name="Org B")
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=org_a,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        # org_b is a distractor — must not appear in the send_event call
        _ = org_b

        with django_capture_on_commit_callbacks(execute=True):
            service.on_member_created(membership)

        call_kwargs = mock_webhook_service.send_event.call_args[1]
        assert call_kwargs["organization"] == org_a

    def test_on_member_created_emission_is_deferred_to_post_commit(
        self, service, mock_webhook_service, organization, user, django_capture_on_commit_callbacks
    ):
        """on_member_created registers the emission as an on_commit callback, not synchronously.

        Capturing callbacks without executing them (execute=False) must show exactly one
        pending callback and NO send_event call.  Only after the callbacks run does the
        emission happen.
        """
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=organization,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )

        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            service.on_member_created(membership)

        # Emission must NOT have happened yet — still inside the test transaction.
        mock_webhook_service.send_event.assert_not_called()
        # Exactly one callback was registered.
        assert len(callbacks) == 1

        # Executing the captured callbacks now fires the emission.
        for cb in callbacks:
            cb()
        mock_webhook_service.send_event.assert_called_once()
