"""Audit-emission tests for OrganizationService write paths.

Each test drives a real OrganizationService (audit_service injected via the DI
container) and asserts that the expected audit record(s) are enqueued. We patch
``vinta_audit_logs.tasks.persist_audit_record`` and execute the on_commit callbacks so the
enqueue happens, then inspect the serialized payloads.
"""

from __future__ import annotations

import datetime
from unittest.mock import Mock, patch

import pytest
from model_bakery import baker

from audit_integration.constants import AuditAction
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import (
    Organization,
    OrganizationInvitation,
)
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.services import OrganizationService
from organizations.tests.helpers import make_membership


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
        user = baker.make("users.user")
        service = self._service()

        with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                org = service.create_organization(creator=user, name="ACME")

        payloads = _payloads(mock_task)
        assert _subjects(mock_task) == {
            "organizations.organization",
            "organizations.organizationmembership",
        }
        # Actor is the creator's freshly-minted admin membership.
        for p in payloads:
            assert p["scope"]["scope_key"] == str(org.id)
            assert p["action_key"] == AuditAction.CREATE
            assert p["actor"]["identity_type"] == "membership"
            assert p["actor"]["identity_key"] == str(user.id)
            # The snapshot records what the membership actually held, not a
            # label derived from it -- there is no role column any more.
            assert GROUP_ORGANIZATION_ADMIN in p["actor"]["group_names"]

    def test_invite_user_records_create(self, django_capture_on_commit_callbacks) -> None:
        org = baker.make(Organization)
        inviter = baker.make("users.user")
        make_membership(
            user=inviter,
            organization=org,
            groups=[GROUP_ORGANIZATION_ADMIN],
        )
        service = self._service()

        with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
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
        assert payloads[0]["subject"]["subject_type"] == "organizations.organizationinvitation"
        assert payloads[0]["action_key"] == AuditAction.CREATE
        assert payloads[0]["actor"]["identity_key"] == str(inviter.id)

    def test_accept_invitation_records_membership_and_invitation(
        self, django_capture_on_commit_callbacks
    ) -> None:
        org = baker.make(Organization)
        user = baker.make("users.user", email="joiner@example.com")
        raw = generate_long_lived_token()
        invitation = OrganizationInvitation.objects.create(
            email="joiner@example.com",
            organization=org,
            token_hash=hash_long_lived_token(raw),
            group=GROUP_ORGANIZATION_MEMBER,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=1),
        )
        service = self._service()

        with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.accept_invitation(token=raw, user=user)

        assert _subjects(mock_task) == {
            "organizations.organizationmembership",
            "organizations.organizationinvitation",
        }
        for p in _payloads(mock_task):
            assert p["scope"]["scope_key"] == str(org.id)
            assert p["actor"]["identity_key"] == str(user.id)
        assert invitation.organization_id == org.id

    def test_revoke_invitation_records_update_with_system_actor(
        self, django_capture_on_commit_callbacks
    ) -> None:
        org = baker.make(Organization)
        invitation = OrganizationInvitation.objects.create(
            email="x@example.com",
            organization=org,
            token_hash="hash",
            group=GROUP_ORGANIZATION_MEMBER,
            expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=5),
        )
        service = self._service()

        with patch("vinta_audit_logs.tasks.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.revoke_invitation(invitation_id=str(invitation.id))

        payloads = _payloads(mock_task)
        assert len(payloads) == 1
        assert payloads[0]["action_key"] == AuditAction.UPDATE
        assert payloads[0]["actor"]["identity_type"] == "system"
        assert "expires_at" in payloads[0]["diff"]
