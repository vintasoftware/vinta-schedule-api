from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from audit.types import (
    ActorSnapshot,
    AuditPage,
    AuditQuery,
    AuditRecord,
    AuditRecordData,
    SubjectRef,
)


class TestActorSnapshot:
    """Tests for ActorSnapshot dataclass."""

    def test_construct(self):
        """ActorSnapshot constructs with all fields."""
        actor = ActorSnapshot(
            actor_type="membership",
            actor_id=123,
            actor_role="admin",
            system_user_scopes=["calendar_event", "calendar"],
            system_user_scoped_to_membership=456,
        )
        assert actor.actor_type == "membership"
        assert actor.actor_id == 123
        assert actor.actor_role == "admin"
        assert actor.system_user_scopes == ["calendar_event", "calendar"]
        assert actor.system_user_scoped_to_membership == 456

    def test_frozen(self):
        """ActorSnapshot is frozen; mutations raise."""
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        with pytest.raises(dataclasses.FrozenInstanceError):
            actor.actor_type = "membership"

    def test_to_dict(self):
        """ActorSnapshot serializes to dict and is JSON-serializable."""
        actor = ActorSnapshot(actor_type="membership", actor_id=123, actor_role="admin")
        d = dataclasses.asdict(actor)
        assert d == {
            "actor_type": "membership",
            "actor_id": 123,
            "actor_role": "admin",
            "system_user_scopes": None,
            "system_user_scoped_to_membership": None,
        }
        # Verify JSON serialization succeeds
        json_str = json.dumps(d)
        assert json.loads(json_str) == d

    def test_defaults(self):
        """ActorSnapshot fields have correct defaults."""
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        assert actor.actor_role is None
        assert actor.system_user_scopes is None
        assert actor.system_user_scoped_to_membership is None


class TestSubjectRef:
    """Tests for SubjectRef dataclass."""

    def test_construct(self):
        """SubjectRef constructs with type, id, and optional label."""
        subject = SubjectRef(
            subject_type="calendar_integration.CalendarEvent",
            subject_id="123",
            subject_label="Team Meeting",
        )
        assert subject.subject_type == "calendar_integration.CalendarEvent"
        assert subject.subject_id == "123"
        assert subject.subject_label == "Team Meeting"

    def test_frozen(self):
        """SubjectRef is frozen."""
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            subject.subject_id = "2"

    def test_to_dict(self):
        """SubjectRef serializes to dict."""
        subject = SubjectRef(subject_type="app.Model", subject_id="1", subject_label="Item")
        d = dataclasses.asdict(subject)
        assert d == {
            "subject_type": "app.Model",
            "subject_id": "1",
            "subject_label": "Item",
        }

    def test_label_optional(self):
        """SubjectRef label defaults to None."""
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        assert subject.subject_label is None


class TestAuditRecordData:
    """Tests for AuditRecordData dataclass."""

    def test_construct(self):
        """AuditRecordData constructs with required and optional fields."""
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        data = AuditRecordData(
            organization_id=1,
            action="create",
            actor=actor,
            subject=subject,
            affected_membership_ids=[1, 2],
            diff={"field": {"old": "a", "new": "b"}},
        )
        assert data.organization_id == 1
        assert data.action == "create"
        assert data.actor == actor
        assert data.subject == subject
        assert data.affected_membership_ids == [1, 2]
        assert data.diff == {"field": {"old": "a", "new": "b"}}

    def test_frozen(self):
        """AuditRecordData is frozen."""
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        data = AuditRecordData(organization_id=1, action="create", actor=actor, subject=subject)
        with pytest.raises(dataclasses.FrozenInstanceError):
            data.organization_id = 2

    def test_defaults(self):
        """AuditRecordData affected_membership_ids and diff default correctly."""
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        data = AuditRecordData(organization_id=1, action="create", actor=actor, subject=subject)
        assert data.affected_membership_ids == []
        assert data.diff is None

    def test_to_dict(self):
        """AuditRecordData is fully JSON-serializable (Celery payload requirement)."""
        actor = ActorSnapshot(
            actor_type="membership",
            actor_id=123,
            actor_role="admin",
            system_user_scopes=["calendar_event", "calendar"],
        )
        subject = SubjectRef(
            subject_type="calendar_integration.CalendarEvent",
            subject_id="456",
            subject_label="Team Meeting",
        )
        data = AuditRecordData(
            organization_id=1,
            action="create",
            actor=actor,
            subject=subject,
            affected_membership_ids=[1, 2, 3],
            diff={"title": {"old": "Old Title", "new": "New Title"}},
        )
        d = dataclasses.asdict(data)
        assert d["organization_id"] == 1
        assert d["action"] == "create"
        assert d["actor"]["actor_type"] == "membership"
        assert d["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
        assert d["affected_membership_ids"] == [1, 2, 3]
        assert d["diff"] == {"title": {"old": "Old Title", "new": "New Title"}}
        # Verify JSON round-trip (Celery serialization requirement)
        json_str = json.dumps(d)
        reconstructed = json.loads(json_str)
        assert reconstructed == d


class TestAuditRecord:
    """Tests for AuditRecord dataclass."""

    def test_construct(self):
        """AuditRecord constructs with all fields including id and created_at."""
        created_at = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        record = AuditRecord(
            id=123,
            created_at=created_at,
            organization_id=1,
            action="create",
            actor=actor,
            subject=subject,
            affected_membership_ids=[1],
        )
        assert record.id == 123
        assert record.created_at == created_at
        assert record.organization_id == 1

    def test_frozen(self):
        """AuditRecord is frozen."""
        created_at = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        record = AuditRecord(
            id=123,
            created_at=created_at,
            organization_id=1,
            action="create",
            actor=actor,
            subject=subject,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.id = 124

    def test_to_dict_datetime_preserved(self):
        """AuditRecord.asdict preserves datetime objects (return type, not Celery payload).

        AuditRecord is a repository return type with id + created_at.
        AuditRecordData (without datetime) is the Celery payload; JSON-serializability
        is tested separately for that type.
        """
        created_at = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        record = AuditRecord(
            id=123,
            created_at=created_at,
            organization_id=1,
            action="create",
            actor=actor,
            subject=subject,
        )
        d = dataclasses.asdict(record)
        # AuditRecord preserves datetime in asdict (repository return type is not JSON-serialized)
        assert d["created_at"] == created_at
        assert isinstance(d["created_at"], datetime)


class TestAuditQuery:
    """Tests for AuditQuery dataclass."""

    def test_all_none(self):
        """AuditQuery with all None fields."""
        q = AuditQuery()
        assert q.organization_id is None
        assert q.actions is None
        assert q.actor_type is None
        assert q.search is None

    def test_selective_filters(self):
        """AuditQuery with only some filters set."""
        q = AuditQuery(
            organization_id=1,
            actions=["create", "update"],
            created_after=datetime(2026, 6, 1, tzinfo=ZoneInfo("UTC")),
        )
        assert q.organization_id == 1
        assert q.actions == ["create", "update"]
        assert q.actor_type is None
        assert q.created_after == datetime(2026, 6, 1, tzinfo=ZoneInfo("UTC"))

    def test_frozen(self):
        """AuditQuery is frozen."""
        q = AuditQuery(organization_id=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            q.organization_id = 2


class TestAuditPage:
    """Tests for AuditPage dataclass."""

    def test_construct(self):
        """AuditPage constructs with items and total."""
        created_at = datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("UTC"))
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        record = AuditRecord(
            id=1,
            created_at=created_at,
            organization_id=1,
            action="create",
            actor=actor,
            subject=subject,
        )
        page = AuditPage(items=[record], total=100)
        assert len(page.items) == 1
        assert page.total == 100

    def test_frozen(self):
        """AuditPage is frozen."""
        page = AuditPage(items=[], total=0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            page.total = 1

    def test_empty_page(self):
        """AuditPage with no items."""
        page = AuditPage(items=[], total=0)
        assert page.items == []
        assert page.total == 0
