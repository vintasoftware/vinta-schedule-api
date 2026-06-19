"""Tests for DjangoORMAuditRepository.add and .get methods.

Covers:
- add() persists all actor snapshot variants (SYSTEM, MEMBERSHIP, SYSTEM_USER,
  SINGLE_USE_CODE) with their respective nullable fields.
- add() persists affected_membership_ids and diff.
- add() returns an AuditRecord that matches the persisted state.
- add() deduplicates duplicate affected_membership_ids without violating the
  unique constraint.
- get() round-trips a full record including affected_membership_ids and diff.
- get() returns None for a missing id.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.models import Audit, AuditAffectedMembership
from audit.repositories import DjangoORMAuditRepository
from audit.types import ActorSnapshot, AuditRecord, AuditRecordData, SubjectRef
from organizations.models import Organization, OrganizationMembership, OrganizationRole


User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_membership(org: Organization) -> OrganizationMembership:
    """Create a user and return their membership in *org*."""
    user = baker.make(User)
    return OrganizationMembership.objects.create(user=user, organization=org)


def make_subject() -> SubjectRef:
    return SubjectRef(
        subject_type="organizations.Organization",
        subject_id="42",
        subject_label="Test Org",
    )


def make_system_actor() -> ActorSnapshot:
    return ActorSnapshot(
        actor_type=AuditActorType.SYSTEM,
        actor_id=None,
    )


# ---------------------------------------------------------------------------
# add() — actor snapshot variants
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDjangoORMAuditRepositoryAdd:
    """Tests that add() persists actor snapshot variants and returns a correct AuditRecord."""

    def test_add_system_actor_persists(self) -> None:
        """SYSTEM actor: actor_id is null, all MEMBERSHIP/SYSTEM_USER fields null."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.SYSTEM,
                actor_id=None,
            ),
            subject=make_subject(),
        )
        record = repo.add(data)

        assert isinstance(record, AuditRecord)
        assert record.actor.actor_type == AuditActorType.SYSTEM
        assert record.actor.actor_id is None
        assert record.actor.actor_role is None
        assert record.actor.system_user_scopes is None
        assert record.actor.system_user_scoped_to_membership is None

        # Confirm DB state matches the DTO.
        db_audit = Audit.original_manager.get(pk=record.id)
        assert db_audit.actor_type == AuditActorType.SYSTEM
        assert db_audit.actor_id is None

    def test_add_membership_actor_persists(self) -> None:
        """MEMBERSHIP actor: actor_id + actor_role populated; system_user_* null."""
        org = baker.make(Organization)
        membership = make_membership(org)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.MEMBERSHIP,
                actor_id=membership.pk,
                actor_role=OrganizationRole.ADMIN,
            ),
            subject=make_subject(),
        )
        record = repo.add(data)

        assert record.actor.actor_type == AuditActorType.MEMBERSHIP
        assert record.actor.actor_id == membership.pk
        assert record.actor.actor_role == OrganizationRole.ADMIN
        assert record.actor.system_user_scopes is None
        assert record.actor.system_user_scoped_to_membership is None

        db_audit = Audit.original_manager.get(pk=record.id)
        assert db_audit.actor_id == membership.pk
        assert db_audit.actor_role == OrganizationRole.ADMIN

    def test_add_system_user_actor_with_scopes_and_scoped_to(self) -> None:
        """SYSTEM_USER actor: scopes list + scoped_to_membership_id populated."""
        org = baker.make(Organization)
        membership = make_membership(org)
        repo = DjangoORMAuditRepository()
        scopes = ["calendar.read", "calendar.write"]

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.SYSTEM_USER,
                actor_id=99,
                system_user_scopes=scopes,
                system_user_scoped_to_membership=membership.pk,
            ),
            subject=make_subject(),
        )
        record = repo.add(data)

        assert record.actor.actor_type == AuditActorType.SYSTEM_USER
        assert record.actor.actor_id == 99
        assert record.actor.system_user_scopes == scopes
        assert record.actor.system_user_scoped_to_membership == membership.pk
        assert record.actor.actor_role is None

        db_audit = Audit.original_manager.get(pk=record.id)
        assert db_audit.system_user_scopes == scopes
        assert db_audit.system_user_scoped_to_membership == membership.pk

    def test_add_system_user_actor_org_wide_scopes(self) -> None:
        """SYSTEM_USER actor with org-wide token: scoped_to_membership is null."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.DELETE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.SYSTEM_USER,
                actor_id=77,
                system_user_scopes=["calendar.read"],
                system_user_scoped_to_membership=None,
            ),
            subject=make_subject(),
        )
        record = repo.add(data)

        assert record.actor.system_user_scoped_to_membership is None
        db_audit = Audit.original_manager.get(pk=record.id)
        assert db_audit.system_user_scoped_to_membership is None

    def test_add_single_use_code_actor_persists(self) -> None:
        """SINGLE_USE_CODE actor: actor_id populated; role and system_user_* null."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.SINGLE_USE_CODE,
                actor_id=5,
            ),
            subject=make_subject(),
        )
        record = repo.add(data)

        assert record.actor.actor_type == AuditActorType.SINGLE_USE_CODE
        assert record.actor.actor_id == 5
        assert record.actor.actor_role is None
        assert record.actor.system_user_scopes is None


# ---------------------------------------------------------------------------
# add() — affected_membership_ids
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDjangoORMAuditRepositoryAddAffectedMemberships:
    """Tests that add() persists affected_membership_ids correctly."""

    def test_add_with_no_affected_memberships(self) -> None:
        """add() with an empty affected_membership_ids list creates no through rows."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=make_system_actor(),
            subject=make_subject(),
            affected_membership_ids=[],
        )
        record = repo.add(data)

        assert record.affected_membership_ids == []
        assert AuditAffectedMembership.original_manager.filter(audit_fk_id=record.id).count() == 0

    def test_add_with_single_affected_membership(self) -> None:
        """add() with one membership id creates one through row."""
        org = baker.make(Organization)
        membership = make_membership(org)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=make_system_actor(),
            subject=make_subject(),
            affected_membership_ids=[membership.pk],
        )
        record = repo.add(data)

        assert record.affected_membership_ids == [membership.pk]

    def test_add_with_multiple_affected_memberships(self) -> None:
        """add() with multiple membership ids creates the correct number of through rows."""
        org = baker.make(Organization)
        m1 = make_membership(org)
        m2 = make_membership(org)
        m3 = make_membership(org)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.DELETE,
            actor=make_system_actor(),
            subject=make_subject(),
            affected_membership_ids=[m1.pk, m2.pk, m3.pk],
        )
        record = repo.add(data)

        assert sorted(record.affected_membership_ids) == sorted([m1.pk, m2.pk, m3.pk])
        assert AuditAffectedMembership.original_manager.filter(audit_fk_id=record.id).count() == 3

    def test_add_deduplicates_duplicate_affected_membership_ids(self) -> None:
        """Duplicate membership ids in affected_membership_ids are silently deduplicated.

        The unique constraint on (organization, audit_fk, membership_fk) would raise an
        IntegrityError if duplicates were passed to bulk_create. The repository must
        deduplicate before inserting.
        """
        org = baker.make(Organization)
        membership = make_membership(org)
        repo = DjangoORMAuditRepository()

        # Pass the same membership id three times.
        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=make_system_actor(),
            subject=make_subject(),
            affected_membership_ids=[membership.pk, membership.pk, membership.pk],
        )
        # Must not raise; should silently deduplicate to one row.
        record = repo.add(data)

        assert record.affected_membership_ids == [membership.pk]
        assert AuditAffectedMembership.original_manager.filter(audit_fk_id=record.id).count() == 1


# ---------------------------------------------------------------------------
# add() — diff field
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDjangoORMAuditRepositoryAddDiff:
    """Tests that add() persists the diff field correctly."""

    def test_add_with_null_diff(self) -> None:
        """add() with diff=None stores null and returns None in the DTO."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=make_system_actor(),
            subject=make_subject(),
            diff=None,
        )
        record = repo.add(data)

        assert record.diff is None
        db_audit = Audit.original_manager.get(pk=record.id)
        assert db_audit.diff is None

    def test_add_with_diff_payload(self) -> None:
        """add() with a diff dict persists and returns it correctly."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()
        diff_payload = {
            "name": {"old": "Alice", "new": "Bob"},
            "role": {"old": "member", "new": "admin"},
        }

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=make_system_actor(),
            subject=make_subject(),
            diff=diff_payload,
        )
        record = repo.add(data)

        assert record.diff == diff_payload
        db_audit = Audit.original_manager.get(pk=record.id)
        assert db_audit.diff == diff_payload

    def test_add_with_empty_dict_diff_normalizes_to_none(self) -> None:
        """add() with diff={} persists null (normalized) so has_diff=False matches it.

        Diff invariant: diff is always either None or a NON-EMPTY dict.  An
        empty dict ({}) carries no change information and is normalized to None
        at write time so that the has_diff filter (diff__isnull) is correct.
        """
        from audit.types import AuditQuery

        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=make_system_actor(),
            subject=make_subject(),
            diff={},
        )
        record = repo.add(data)

        # The DTO should reflect the normalized value.
        assert record.diff is None

        # The DB row must also have diff=NULL.
        db_audit = Audit.original_manager.get(pk=record.id)
        assert db_audit.diff is None

        # has_diff=False must include this record; has_diff=True must exclude it.
        page_no_diff = repo.query(AuditQuery(organization_id=org.pk, has_diff=False))
        assert any(r.id == record.id for r in page_no_diff.items)

        page_with_diff = repo.query(AuditQuery(organization_id=org.pk, has_diff=True))
        assert all(r.id != record.id for r in page_with_diff.items)


# ---------------------------------------------------------------------------
# add() — returned AuditRecord completeness
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDjangoORMAuditRepositoryAddReturnValue:
    """Tests that the AuditRecord returned by add() matches persisted DB state."""

    def test_add_returns_record_with_id_and_created_at(self) -> None:
        """add() returns a record with a real database id and auto-populated created_at."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=make_system_actor(),
            subject=make_subject(),
        )
        record = repo.add(data)

        assert record.id > 0
        assert record.created_at is not None

    def test_add_returned_record_matches_db_row(self) -> None:
        """Every field of the returned AuditRecord matches the persisted DB row."""
        org = baker.make(Organization)
        membership = make_membership(org)
        repo = DjangoORMAuditRepository()
        subject = SubjectRef(
            subject_type="calendar_integration.CalendarEvent",
            subject_id="100",
            subject_label="Board Meeting",
        )
        diff = {"title": {"old": "Meeting", "new": "Board Meeting"}}

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.MEMBERSHIP,
                actor_id=membership.pk,
                actor_role=OrganizationRole.MEMBER,
            ),
            subject=subject,
            affected_membership_ids=[membership.pk],
            diff=diff,
        )
        record = repo.add(data)

        # Cross-check against DB.
        db_audit = Audit.original_manager.get(pk=record.id)
        assert record.organization_id == org.pk
        assert record.action == AuditAction.UPDATE
        assert record.actor.actor_type == AuditActorType.MEMBERSHIP
        assert record.actor.actor_id == membership.pk
        assert record.actor.actor_role == OrganizationRole.MEMBER
        assert record.subject.subject_type == "calendar_integration.CalendarEvent"
        assert record.subject.subject_id == "100"
        assert record.subject.subject_label == "Board Meeting"
        assert record.affected_membership_ids == [membership.pk]
        assert record.diff == diff
        assert record.created_at == db_audit.created_at


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDjangoORMAuditRepositoryGet:
    """Tests for DjangoORMAuditRepository.get()."""

    def test_get_returns_none_for_missing_id(self) -> None:
        """get() returns None when no Audit row has the given id."""
        repo = DjangoORMAuditRepository()
        result = repo.get(audit_id=999_999_999)
        assert result is None

    def test_get_round_trips_full_record(self) -> None:
        """add() then get() returns an identical AuditRecord."""
        org = baker.make(Organization)
        membership = make_membership(org)
        repo = DjangoORMAuditRepository()
        subject = SubjectRef(
            subject_type="organizations.OrganizationMembership",
            subject_id=str(membership.pk),
            subject_label="Test Member",
        )
        diff = {"role": {"old": "member", "new": "admin"}}

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=ActorSnapshot(
                actor_type=AuditActorType.SYSTEM_USER,
                actor_id=55,
                system_user_scopes=["scheduling.read"],
                system_user_scoped_to_membership=membership.pk,
            ),
            subject=subject,
            affected_membership_ids=[membership.pk],
            diff=diff,
        )

        added = repo.add(data)
        fetched = repo.get(audit_id=added.id)

        assert fetched is not None
        assert fetched.id == added.id
        assert fetched.created_at == added.created_at
        assert fetched.organization_id == added.organization_id
        assert fetched.action == added.action
        assert fetched.actor == added.actor
        assert fetched.subject == added.subject
        assert fetched.affected_membership_ids == added.affected_membership_ids
        assert fetched.diff == added.diff

    def test_get_round_trips_record_with_no_affected_memberships(self) -> None:
        """get() returns an empty affected_membership_ids list when none were added."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=make_system_actor(),
            subject=make_subject(),
            affected_membership_ids=[],
        )
        added = repo.add(data)
        fetched = repo.get(audit_id=added.id)

        assert fetched is not None
        assert fetched.affected_membership_ids == []

    def test_get_round_trips_diff(self) -> None:
        """get() returns the same diff dict that was persisted by add()."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()
        diff = {"email": {"old": "a@example.com", "new": "b@example.com"}}

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=make_system_actor(),
            subject=make_subject(),
            diff=diff,
        )
        added = repo.add(data)
        fetched = repo.get(audit_id=added.id)

        assert fetched is not None
        assert fetched.diff == diff

    def test_get_round_trips_null_diff(self) -> None:
        """get() returns None for diff when none was provided."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        data = AuditRecordData(
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=make_system_actor(),
            subject=make_subject(),
            diff=None,
        )
        added = repo.add(data)
        fetched = repo.get(audit_id=added.id)

        assert fetched is not None
        assert fetched.diff is None
