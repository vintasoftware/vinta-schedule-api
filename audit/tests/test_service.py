"""Tests for AuditService.

Covers:
- Actor builder staticmethods capture correct snapshots synchronously.
- record() enqueues a JSON-serializable payload with correct field mapping.
- record() swallows and logs an enqueue error without re-raising.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.repositories import DjangoORMAuditRepository
from audit.services import AuditService
from audit.types import SubjectRef
from organizations.models import Organization, OrganizationMembership, OrganizationRole


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_service() -> AuditService:
    """Instantiate AuditService with a real ORM repository (DI bypassed for unit tests)."""
    repository = DjangoORMAuditRepository()
    # Bypass DI by constructing directly.
    service = object.__new__(AuditService)
    service.repository = repository
    return service


def make_subject() -> SubjectRef:
    return SubjectRef(
        subject_type="organizations.Organization",
        subject_id="1",
        subject_label="Test Org",
    )


# ---------------------------------------------------------------------------
# Actor builder helpers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestActorFromMembership:
    """actor_from_membership captures role and id synchronously."""

    def test_sets_actor_type_membership(self) -> None:
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.MEMBER
        )

        snapshot = AuditService.actor_from_membership(membership)

        assert snapshot.actor_type == AuditActorType.MEMBERSHIP

    def test_captures_actor_id(self) -> None:
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(user=user, organization=org)

        snapshot = AuditService.actor_from_membership(membership)

        assert snapshot.actor_id == membership.user_id

    def test_captures_role_at_call_time(self) -> None:
        """actor_role is snapshotted at build time, not at task execution time."""
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.MEMBER
        )

        snapshot = AuditService.actor_from_membership(membership)

        assert snapshot.actor_role == OrganizationRole.MEMBER

    def test_no_system_user_scopes(self) -> None:
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(user=user, organization=org)

        snapshot = AuditService.actor_from_membership(membership)

        assert snapshot.system_user_scopes is None
        assert snapshot.system_user_scoped_to_membership is None


@pytest.mark.django_db
class TestActorFromSystemUser:
    """actor_from_system_user captures scopes and scoped_to_membership synchronously."""

    def test_sets_actor_type_system_user(self) -> None:
        from public_api.models import SystemUser

        org = baker.make(Organization)
        system_user = SystemUser.objects.create(
            organization=org,
            integration_name="test_integration",
            long_lived_token_hash="abc123",
        )

        snapshot = AuditService.actor_from_system_user(system_user)

        assert snapshot.actor_type == AuditActorType.SYSTEM_USER

    def test_captures_actor_id(self) -> None:
        from public_api.models import SystemUser

        org = baker.make(Organization)
        system_user = SystemUser.objects.create(
            organization=org,
            integration_name="test_integration_id",
            long_lived_token_hash="abc123",
        )

        snapshot = AuditService.actor_from_system_user(system_user)

        assert snapshot.actor_id == system_user.id

    def test_captures_scopes_at_call_time(self) -> None:
        """system_user_scopes are read from available_resources now, not in the worker."""
        from public_api.constants import PublicAPIResources
        from public_api.models import ResourceAccess, SystemUser

        org = baker.make(Organization)
        system_user = SystemUser.objects.create(
            organization=org,
            integration_name="test_integration_scopes",
            long_lived_token_hash="abc123",
        )
        ResourceAccess.objects.create(
            system_user=system_user, resource_name=PublicAPIResources.CALENDAR_EVENT
        )
        ResourceAccess.objects.create(
            system_user=system_user, resource_name=PublicAPIResources.CALENDAR
        )

        snapshot = AuditService.actor_from_system_user(system_user)

        assert sorted(snapshot.system_user_scopes or []) == sorted(
            [PublicAPIResources.CALENDAR_EVENT, PublicAPIResources.CALENDAR]
        )

    def test_empty_scopes_when_no_resources(self) -> None:
        from public_api.models import SystemUser

        org = baker.make(Organization)
        system_user = SystemUser.objects.create(
            organization=org,
            integration_name="test_integration_noscopes",
            long_lived_token_hash="abc123",
        )

        snapshot = AuditService.actor_from_system_user(system_user)

        assert snapshot.system_user_scopes == []

    def test_captures_scoped_to_membership_user_id(self) -> None:
        from public_api.models import SystemUser

        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(user=user, organization=org)
        system_user = SystemUser.objects.create(
            organization=org,
            integration_name="test_integration_scoped",
            long_lived_token_hash="abc123",
            scoped_to_membership_user_id=membership.user_id,
        )

        snapshot = AuditService.actor_from_system_user(system_user)

        # snapshot now stores the org-scoped user_id (OrganizationMembershipForeignKey convention)
        assert snapshot.system_user_scoped_to_membership == membership.user_id

    def test_scoped_to_membership_is_none_for_org_wide_token(self) -> None:
        from public_api.models import SystemUser

        org = baker.make(Organization)
        system_user = SystemUser.objects.create(
            organization=org,
            integration_name="test_integration_orgwide",
            long_lived_token_hash="abc123",
        )

        snapshot = AuditService.actor_from_system_user(system_user)

        assert snapshot.system_user_scoped_to_membership is None

    def test_no_actor_role(self) -> None:
        from public_api.models import SystemUser

        org = baker.make(Organization)
        system_user = SystemUser.objects.create(
            organization=org,
            integration_name="test_integration_norole",
            long_lived_token_hash="abc123",
        )

        snapshot = AuditService.actor_from_system_user(system_user)

        assert snapshot.actor_role is None


@pytest.mark.django_db
class TestActorFromSingleUseCode:
    """actor_from_single_use_code captures token id."""

    def test_sets_actor_type_single_use_code(self) -> None:
        from calendar_integration.models import CalendarManagementToken

        org = baker.make(Organization)
        token = baker.make(CalendarManagementToken, organization=org)

        snapshot = AuditService.actor_from_single_use_code(token)

        assert snapshot.actor_type == AuditActorType.SINGLE_USE_CODE

    def test_captures_actor_id(self) -> None:
        from calendar_integration.models import CalendarManagementToken

        org = baker.make(Organization)
        token = baker.make(CalendarManagementToken, organization=org)

        snapshot = AuditService.actor_from_single_use_code(token)

        assert snapshot.actor_id == token.id

    def test_no_actor_role_or_scopes(self) -> None:
        from calendar_integration.models import CalendarManagementToken

        org = baker.make(Organization)
        token = baker.make(CalendarManagementToken, organization=org)

        snapshot = AuditService.actor_from_single_use_code(token)

        assert snapshot.actor_role is None
        assert snapshot.system_user_scopes is None
        assert snapshot.system_user_scoped_to_membership is None


class TestSystemActor:
    """system_actor returns a SYSTEM snapshot with actor_id=None."""

    def test_sets_actor_type_system(self) -> None:
        snapshot = AuditService.system_actor()
        assert snapshot.actor_type == AuditActorType.SYSTEM

    def test_actor_id_is_none(self) -> None:
        snapshot = AuditService.system_actor()
        assert snapshot.actor_id is None

    def test_no_role_or_scopes(self) -> None:
        snapshot = AuditService.system_actor()
        assert snapshot.actor_role is None
        assert snapshot.system_user_scopes is None
        assert snapshot.system_user_scoped_to_membership is None


# ---------------------------------------------------------------------------
# record() — payload shape and JSON-serializability
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordEnqueues:
    """record() enqueues a correct JSON-serializable payload."""

    def test_enqueues_with_json_serializable_payload(
        self, django_capture_on_commit_callbacks
    ) -> None:
        """The payload passed to persist_audit_record.delay() must be JSON-safe."""
        service = make_service()
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(user=user, organization=org)
        actor = AuditService.actor_from_membership(membership)
        subject = make_subject()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.record(
                    organization_id=org.pk,
                    action=AuditAction.UPDATE,
                    actor=actor,
                    subject=subject,
                    affected_membership_ids=[membership.user_id],
                    diff={"name": {"old": "Alice", "new": "Bob"}},
                )

        mock_task.delay.assert_called_once()
        payload = mock_task.delay.call_args[0][0]

        # Must be JSON-serializable.
        json_str = json.dumps(payload)
        round_tripped = json.loads(json_str)
        assert round_tripped == payload

    def test_payload_has_correct_actor_fields(self, django_capture_on_commit_callbacks) -> None:
        service = make_service()
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.ADMIN
        )
        actor = AuditService.actor_from_membership(membership)
        subject = make_subject()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.record(
                    organization_id=org.pk,
                    action=AuditAction.CREATE,
                    actor=actor,
                    subject=subject,
                )

        payload = mock_task.delay.call_args[0][0]
        assert payload["actor"]["actor_type"] == AuditActorType.MEMBERSHIP
        assert payload["actor"]["actor_id"] == membership.user_id
        assert payload["actor"]["actor_role"] == OrganizationRole.ADMIN

    def test_payload_has_correct_subject_fields(self, django_capture_on_commit_callbacks) -> None:
        service = make_service()
        org = baker.make(Organization)
        actor = AuditService.system_actor()
        subject = SubjectRef(
            subject_type="organizations.Organization",
            subject_id=str(org.pk),
            subject_label="ACME Corp",
        )

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.record(
                    organization_id=org.pk,
                    action=AuditAction.DELETE,
                    actor=actor,
                    subject=subject,
                )

        payload = mock_task.delay.call_args[0][0]
        assert payload["subject"]["subject_type"] == "organizations.Organization"
        assert payload["subject"]["subject_id"] == str(org.pk)
        assert payload["subject"]["subject_label"] == "ACME Corp"

    def test_payload_affected_membership_ids_is_list(
        self, django_capture_on_commit_callbacks
    ) -> None:
        service = make_service()
        org = baker.make(Organization)
        actor = AuditService.system_actor()
        subject = make_subject()
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(user=user, organization=org)

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.record(
                    organization_id=org.pk,
                    action=AuditAction.UPDATE,
                    actor=actor,
                    subject=subject,
                    affected_membership_ids=(membership.user_id,),  # tuple input
                )

        payload = mock_task.delay.call_args[0][0]
        assert isinstance(payload["affected_membership_ids"], list)
        assert payload["affected_membership_ids"] == [membership.user_id]

    def test_payload_diff_is_present(self, django_capture_on_commit_callbacks) -> None:
        service = make_service()
        org = baker.make(Organization)
        actor = AuditService.system_actor()
        subject = make_subject()
        diff = {"role": {"old": "member", "new": "admin"}}

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.record(
                    organization_id=org.pk,
                    action=AuditAction.UPDATE,
                    actor=actor,
                    subject=subject,
                    diff=diff,
                )

        payload = mock_task.delay.call_args[0][0]
        assert payload["diff"] == diff

    def test_payload_organization_id_and_action(self, django_capture_on_commit_callbacks) -> None:
        service = make_service()
        org = baker.make(Organization)
        actor = AuditService.system_actor()
        subject = make_subject()

        with patch("audit.services.persist_audit_record") as mock_task:
            with django_capture_on_commit_callbacks(execute=True):
                service.record(
                    organization_id=org.pk,
                    action=AuditAction.CREATE,
                    actor=actor,
                    subject=subject,
                )

        payload = mock_task.delay.call_args[0][0]
        assert payload["organization_id"] == org.pk
        assert payload["action"] == AuditAction.CREATE


# ---------------------------------------------------------------------------
# record() — error isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecordSwallowsEnqueueError:
    """record() must not propagate broker / enqueue errors to the caller."""

    def test_record_does_not_raise_on_delay_error(
        self, caplog, django_capture_on_commit_callbacks
    ) -> None:
        """A broker error during .delay() is swallowed; record() returns None."""
        service = make_service()
        org = baker.make(Organization)
        actor = AuditService.system_actor()
        subject = make_subject()

        with patch("audit.services.persist_audit_record") as mock_task:
            mock_task.delay.side_effect = RuntimeError("broker unavailable")

            with caplog.at_level(logging.ERROR, logger="audit.services"):
                with django_capture_on_commit_callbacks(execute=True):
                    result = service.record(
                        organization_id=org.pk,
                        action=AuditAction.CREATE,
                        actor=actor,
                        subject=subject,
                    )

        assert result is None  # fire-and-forget, always returns None

    def test_record_logs_on_delay_error(self, caplog, django_capture_on_commit_callbacks) -> None:
        """An enqueue error must be logged (not silently swallowed)."""
        service = make_service()
        org = baker.make(Organization)
        actor = AuditService.system_actor()
        subject = make_subject()

        with patch("audit.services.persist_audit_record") as mock_task:
            mock_task.delay.side_effect = OSError("connection refused")

            with caplog.at_level(logging.ERROR, logger="audit.services"):
                with django_capture_on_commit_callbacks(execute=True):
                    service.record(
                        organization_id=org.pk,
                        action=AuditAction.CREATE,
                        actor=actor,
                        subject=subject,
                    )

        assert any("Failed to enqueue audit record" in r.message for r in caplog.records)
