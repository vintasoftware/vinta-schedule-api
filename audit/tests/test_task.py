"""Tests for persist_audit_record Celery task.

With CELERY_TASK_ALWAYS_EAGER = True (set in test settings), calling
.delay() runs the task body synchronously in the same process, so we can
assert DB state immediately after the call.

Covers:
- persist_audit_record writes a correct Audit row and affected membership
  links through the real ORM repository.
- Snapshot-at-emit proof: the actor_role is the role at build time, not at
  task execution time — even if the membership's role changed between the
  snapshot and the task running.
- A malformed payload is logged and swallowed (no exception propagated).
- A repository failure is logged and swallowed.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.models import Audit, AuditAffectedMembership
from audit.services import AuditService
from audit.tasks import persist_audit_record
from audit.types import ActorSnapshot, AuditRecordData, SubjectRef
from organizations.models import Organization, OrganizationMembership, OrganizationRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_payload(data: AuditRecordData) -> dict:
    """Serialize AuditRecordData to the JSON-safe dict the task expects."""
    return dataclasses.asdict(data)


def make_subject(org: Organization) -> SubjectRef:
    return SubjectRef(
        subject_type="organizations.Organization",
        subject_id=str(org.pk),
        subject_label="Test Org",
    )


# ---------------------------------------------------------------------------
# Happy-path persistence
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPersistAuditRecordTask:
    """Running persist_audit_record (eager) writes through the real ORM repository."""

    def test_persists_system_actor(self) -> None:
        """SYSTEM actor: persist_audit_record creates a correct Audit row."""
        org = baker.make(Organization)
        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=make_subject(org),
        )
        payload = build_payload(data)

        persist_audit_record.delay(payload)

        audit = Audit.original_manager.filter(organization_id=org.pk).first()
        assert audit is not None
        assert audit.actor_type == AuditActorType.SYSTEM
        assert audit.actor_id is None
        assert audit.action == AuditAction.CREATE

    def test_persists_membership_actor(self) -> None:
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.ADMIN
        )
        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.MEMBERSHIP,
                actor_id=membership.user_id,
                actor_role=OrganizationRole.ADMIN,
            ),
            subject=make_subject(org),
        )
        payload = build_payload(data)

        persist_audit_record.delay(payload)

        audit = Audit.original_manager.filter(organization_id=org.pk).first()
        assert audit is not None
        assert audit.actor_type == AuditActorType.MEMBERSHIP
        assert audit.actor_id == membership.user_id
        assert audit.actor_role == OrganizationRole.ADMIN

    def test_persists_affected_membership_ids(self) -> None:
        """Affected membership links are created in the through table."""
        org = baker.make(Organization)
        user1 = baker.make("users.User")
        user2 = baker.make("users.User")
        m1 = OrganizationMembership.objects.create(user=user1, organization=org)
        m2 = OrganizationMembership.objects.create(user=user2, organization=org)

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=make_subject(org),
            # affected_membership_ids are now org-scoped user_ids
            affected_membership_ids=[m1.user_id, m2.user_id],
        )
        payload = build_payload(data)

        persist_audit_record.delay(payload)

        audit = Audit.original_manager.filter(organization_id=org.pk).first()
        assert audit is not None
        link_user_ids = set(
            AuditAffectedMembership.original_manager.filter(audit_fk_id=audit.pk).values_list(
                "membership_user_id", flat=True
            )
        )
        assert link_user_ids == {m1.user_id, m2.user_id}

    def test_persists_diff(self) -> None:
        org = baker.make(Organization)
        diff = {"name": {"old": "Alice", "new": "Bob"}}
        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=make_subject(org),
            diff=diff,
        )
        payload = build_payload(data)

        persist_audit_record.delay(payload)

        audit = Audit.original_manager.filter(organization_id=org.pk).first()
        assert audit is not None
        assert audit.diff == diff

    def test_persists_system_user_actor_with_scopes(self) -> None:
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(user=user, organization=org)
        scopes = ["calendar_event", "calendar"]

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.SYSTEM_USER,
                actor_id=42,
                system_user_scopes=scopes,
                # scoped_to_membership now stores the org-scoped user_id
                system_user_scoped_to_membership=membership.user_id,
            ),
            subject=make_subject(org),
        )
        payload = build_payload(data)

        persist_audit_record.delay(payload)

        audit = Audit.original_manager.filter(organization_id=org.pk).first()
        assert audit is not None
        assert audit.actor_type == AuditActorType.SYSTEM_USER
        assert audit.system_user_scopes == scopes
        assert audit.system_user_scoped_to_membership == membership.user_id

    def test_full_round_trip_via_service_record(self, django_capture_on_commit_callbacks) -> None:
        """AuditService.record() + eager task results in a correct persisted Audit."""
        from audit.repositories import DjangoORMAuditRepository

        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.MEMBER
        )

        repository = DjangoORMAuditRepository()
        service = object.__new__(AuditService)
        service.repository = repository

        actor = AuditService.actor_from_membership(membership)
        subject = SubjectRef(
            subject_type="organizations.OrganizationMembership",
            subject_id=str(membership.user_id),
        )
        diff = {"role": {"old": "member", "new": "admin"}}

        # record() registers an on_commit callback; execute=True fires it immediately
        # so the eager task runs and we can assert DB state right after.
        with django_capture_on_commit_callbacks(execute=True):
            service.record(
                organization_id=org.pk,
                action=AuditAction.UPDATE,
                actor=actor,
                subject=subject,
                # affected_membership_ids are now org-scoped user_ids
                affected_membership_ids=[membership.user_id],
                diff=diff,
            )

        audit = Audit.original_manager.filter(organization_id=org.pk).first()
        assert audit is not None
        assert audit.actor_type == AuditActorType.MEMBERSHIP
        assert audit.actor_id == membership.user_id
        assert audit.actor_role == OrganizationRole.MEMBER
        assert audit.diff == diff
        assert AuditAffectedMembership.original_manager.filter(
            audit_fk_id=audit.pk, membership_user_id=membership.user_id
        ).exists()


# ---------------------------------------------------------------------------
# Snapshot-at-emit proof
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSnapshotAtEmitProof:
    """The actor_role in the persisted record is from the snapshot, not the current state.

    Sequence:
    1. Create a membership with role=MEMBER.
    2. Build an ActorSnapshot (captures role=MEMBER synchronously).
    3. Change membership.role to ADMIN in the DB.
    4. Run the task with the payload built from step 2.
    5. Assert the persisted Audit.actor_role == MEMBER (the OLD role).
    """

    def test_persisted_role_is_snapshot_not_current(self) -> None:
        org = baker.make(Organization)
        user = baker.make("users.User")
        membership = OrganizationMembership.objects.create(
            user=user, organization=org, role=OrganizationRole.MEMBER
        )

        # Step 2: snapshot captures MEMBER role right now.
        actor = AuditService.actor_from_membership(membership)
        assert actor.actor_role == OrganizationRole.MEMBER

        # Step 3: change role to ADMIN in the DB AFTER the snapshot was built.
        membership.role = OrganizationRole.ADMIN
        membership.save(update_fields=["role"])

        # Confirm the DB now has ADMIN.
        membership.refresh_from_db()
        assert membership.role == OrganizationRole.ADMIN

        # Step 4: build payload from the snapshot and run the task.
        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=actor,  # actor_role=MEMBER from the old snapshot
            subject=make_subject(org),
        )
        payload = build_payload(data)
        persist_audit_record.delay(payload)

        # Step 5: the persisted row must have the SNAPSHOTTED role (MEMBER), not ADMIN.
        audit = Audit.original_manager.filter(organization_id=org.pk).first()
        assert audit is not None
        assert audit.actor_role == OrganizationRole.MEMBER, (
            f"Expected snapshotted role {OrganizationRole.MEMBER!r} "
            f"but found {audit.actor_role!r}. "
            "The worker must never re-read mutable actor state."
        )


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPersistAuditRecordErrorHandling:
    """Task failures are logged and swallowed without crashing the worker."""

    def test_malformed_payload_is_logged_and_swallowed(self, caplog) -> None:
        """A payload missing required keys logs an error and does not re-raise.

        We pass a real DjangoORMAuditRepository so the malformed-payload path
        (not the None-guard path) is exercised.

        Note: repository= is passed explicitly here to isolate the error branch.
        Injection itself is proven by the happy-path test test_full_round_trip_via_service_record,
        which goes record() → on_commit → .delay() → task with @inject and NO explicit repository.
        """
        from audit.repositories import DjangoORMAuditRepository

        repository = DjangoORMAuditRepository()

        with caplog.at_level(logging.ERROR, logger="audit.tasks"):
            # Call the task function directly, injecting the repository explicitly,
            # so we exercise the malformed-payload error path.
            persist_audit_record({"bad": "payload"}, repository=repository)

        assert any("malformed payload" in r.message for r in caplog.records)

    def test_task_swallows_repository_failure(self, caplog) -> None:
        """A repository.add() failure is logged and swallowed.

        Note: repository= is passed explicitly here to isolate the repository-failure branch.
        Injection itself is proven by the happy-path test test_full_round_trip_via_service_record,
        which goes record() → on_commit → .delay() → task with @inject and NO explicit repository.
        """
        from unittest.mock import MagicMock

        org = baker.make(Organization)
        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=make_subject(org),
        )
        payload = build_payload(data)

        failing_repository = MagicMock()
        failing_repository.add.side_effect = RuntimeError("database unavailable")

        with caplog.at_level(logging.ERROR, logger="audit.tasks"):
            # Pass the failing repository directly (bypassing DI injection).
            persist_audit_record(payload, repository=failing_repository)

        assert any("repository.add() failed" in r.message for r in caplog.records)

    def test_none_repository_guard_logs_and_returns(self, caplog) -> None:
        """When repository=None (DI not wired), the task logs and returns without writing."""
        org = baker.make(Organization)
        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=make_subject(org),
        )
        payload = build_payload(data)

        with caplog.at_level(logging.ERROR, logger="audit.tasks"):
            persist_audit_record(payload, repository=None)

        assert any("repository is not injected" in r.message for r in caplog.records)
        # No Audit row was created.
        assert not Audit.original_manager.filter(organization_id=org.pk).exists()
