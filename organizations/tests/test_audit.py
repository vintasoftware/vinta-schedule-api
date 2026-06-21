"""Audit-emission tests for OrganizationService write paths.

Each test drives a real OrganizationService (audit_service injected via the DI
container) and asserts that the expected audit record(s) are enqueued. We patch
``audit.services.persist_audit_record`` and execute the on_commit callbacks so the
enqueue happens, then inspect the serialized payloads.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from model_bakery import baker

from audit.constants import AuditAction
from organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationRole,
)
from organizations.services import OrganizationService


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


def _subjects(mock_task) -> set[str]:
    return {p["subject"]["subject_type"] for p in _payloads(mock_task)}


@pytest.mark.django_db
class TestOrganizationServiceAudit:
    def _service(self) -> OrganizationService:
        from di_core.containers import container

        with container.calendar_service.override(Mock()):
            return OrganizationService()

    def test_create_organization_records_org_and_membership(
        self, django_capture_on_commit_callbacks
    ) -> None:
        user = baker.make("users.User")
        service = self._service()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                org = service.create_organization(creator=user, name="ACME")

        payloads = _payloads(mock_task)
        assert _subjects(mock_task) == {
            "organizations.Organization",
            "organizations.OrganizationMembership",
        }
        # Actor is the creator's freshly-minted admin membership.
        for p in payloads:
            assert p["organization_id"] == org.id
            assert p["action"] == AuditAction.CREATE
            assert p["actor"]["actor_type"] == "membership"
            assert p["actor"]["actor_id"] == user.id
            assert p["actor"]["actor_role"] == OrganizationRole.ADMIN

    def test_invite_user_records_create(self, django_capture_on_commit_callbacks) -> None:
        org = baker.make(Organization)
        inviter = baker.make("users.User")
        baker.make(
            "organizations.OrganizationMembership",
            user=inviter,
            organization=org,
            role=OrganizationRole.ADMIN,
        )
        service = self._service()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.invite_user_to_organization(
                    email="new@example.com",
                    organization=org,
                    invited_by=inviter,
                    first_name="New",
                    last_name="User",
                    send_email=False,
                )

        payloads = _payloads(mock_task)
        assert len(payloads) == 1
        assert payloads[0]["subject"]["subject_type"] == "organizations.OrganizationInvitation"
        assert payloads[0]["action"] == AuditAction.CREATE
        assert payloads[0]["actor"]["actor_id"] == inviter.id

    def test_accept_invitation_records_membership_and_invitation(
        self, django_capture_on_commit_callbacks
    ) -> None:
        from common.utils.authentication_utils import (
            generate_long_lived_token,
            hash_long_lived_token,
        )

        org = baker.make(Organization)
        user = baker.make("users.User", email="joiner@example.com")
        raw = generate_long_lived_token()
        import datetime

        invitation = OrganizationInvitation.objects.create(
            email="joiner@example.com",
            organization=org,
            token_hash=hash_long_lived_token(raw),
            role=OrganizationRole.MEMBER,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=1),
        )
        service = self._service()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.accept_invitation(token=raw, user=user)

        assert _subjects(mock_task) == {
            "organizations.OrganizationMembership",
            "organizations.OrganizationInvitation",
        }
        for p in _payloads(mock_task):
            assert p["organization_id"] == org.id
            assert p["actor"]["actor_id"] == user.id
        assert invitation.organization_id == org.id

    def test_revoke_invitation_records_update_with_system_actor(
        self, django_capture_on_commit_callbacks
    ) -> None:
        import datetime

        org = baker.make(Organization)
        invitation = OrganizationInvitation.objects.create(
            email="x@example.com",
            organization=org,
            token_hash="hash",
            role=OrganizationRole.MEMBER,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=5),
        )
        service = self._service()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.revoke_invitation(invitation_id=str(invitation.id))

        payloads = _payloads(mock_task)
        assert len(payloads) == 1
        assert payloads[0]["action"] == AuditAction.UPDATE
        assert payloads[0]["actor"]["actor_type"] == "system"
        assert "expires_at" in payloads[0]["diff"]
