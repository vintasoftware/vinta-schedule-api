from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from audit.repositories import AuditRepository
from audit.types import (
    ActorSnapshot,
    AuditPage,
    AuditQuery,
    AuditRecord,
    AuditRecordData,
    SubjectRef,
)


class TestAuditRepositoryAbstract:
    """Tests for AuditRepository ABC."""

    def test_cannot_instantiate(self):
        """AuditRepository cannot be instantiated directly."""
        with pytest.raises(TypeError, match="abstract"):
            AuditRepository()

    @pytest.mark.parametrize("name", ["add", "bulk_add", "get", "query"])
    def test_abstract_methods(self, name):
        """Every backend must implement the append and read primitives itself."""
        assert getattr(getattr(AuditRepository, name), "__isabstractmethod__", False)

    @pytest.mark.parametrize("name", ["get_by_uid", "count", "iter_records"])
    def test_concrete_methods_provided_by_the_interface(self, name):
        """The portable read helpers ship with the interface.

        They are written in terms of ``query``, so a backend gets identity
        lookup, counting and full-log streaming without implementing anything —
        and can still override any of them to hit a native index.
        """
        assert hasattr(AuditRepository, name)
        assert not getattr(getattr(AuditRepository, name), "__isabstractmethod__", False)

    def test_no_update_method(self):
        """AuditRepository does not have update method."""
        assert not hasattr(AuditRepository, "update")

    def test_no_delete_method(self):
        """AuditRepository does not have delete method."""
        assert not hasattr(AuditRepository, "delete")


class StubAuditRepository(AuditRepository):
    """Minimal stub implementation of AuditRepository for testing."""

    def add(self, data: AuditRecordData) -> AuditRecord:
        """Stub: return a record with placeholder id and timestamp."""
        return AuditRecord(
            id=1,
            uid=data.uid,
            created_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
            organization_id=data.organization_id,
            action=data.action,
            actor=data.actor,
            subject=data.subject,
            affected_membership_ids=data.affected_membership_ids,
            diff=data.diff,
        )

    def bulk_add(self, data: Sequence[AuditRecordData]) -> list[AuditRecord]:
        """Stub: map each input through add()."""
        return [self.add(item) for item in data]

    def get(self, audit_id: int) -> AuditRecord | None:
        """Stub: return None."""
        return None

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str | Sequence[str] = ("-created_at",),
    ) -> AuditPage:
        """Stub: return empty page."""
        return AuditPage(items=[], total=0)


class TestStubRepository:
    """Tests for a minimal stub implementation."""

    def test_stub_can_instantiate(self):
        """A concrete subclass implementing all abstract methods can be instantiated."""
        repo = StubAuditRepository()
        assert isinstance(repo, AuditRepository)

    def test_stub_add_returns_record(self):
        """Stub.add returns an AuditRecord."""
        repo = StubAuditRepository()
        actor = ActorSnapshot(actor_type="system", actor_id=None)
        subject = SubjectRef(subject_type="app.Model", subject_id="1")
        data = AuditRecordData(
            organization_id=1,
            action="create",
            actor=actor,
            subject=subject,
        )
        record = repo.add(data)
        assert isinstance(record, AuditRecord)
        assert record.id == 1
        assert record.action == "create"

    def test_stub_add_carries_the_uid_through(self):
        """A repository must store the uid it is given, not mint its own.

        The whole cross-repository design rests on this: a record written to two
        backends has to come back with the same uid from both, or nothing can
        match the copies up.
        """
        repo = StubAuditRepository()
        data = AuditRecordData(
            organization_id=1,
            action="create",
            actor=ActorSnapshot(actor_type="system", actor_id=None),
            subject=SubjectRef(subject_type="app.Model", subject_id="1"),
        )
        assert repo.add(data).uid == data.uid

    def test_stub_bulk_add_returns_one_record_per_input(self):
        """bulk_add returns the records in the order it was given them."""
        repo = StubAuditRepository()
        data = [
            AuditRecordData(
                organization_id=1,
                action=action,
                actor=ActorSnapshot(actor_type="system", actor_id=None),
                subject=SubjectRef(subject_type="app.Model", subject_id="1"),
            )
            for action in ("create", "update", "delete")
        ]
        records = repo.bulk_add(data)
        assert [r.action for r in records] == ["create", "update", "delete"]
        assert [r.uid for r in records] == [d.uid for d in data]

    def test_stub_get_returns_none(self):
        """Stub.get returns None."""
        repo = StubAuditRepository()
        result = repo.get(999)
        assert result is None

    def test_stub_query_returns_empty_page(self):
        """Stub.query returns an AuditPage."""
        repo = StubAuditRepository()
        q = AuditQuery(organization_id=1)
        page = repo.query(q)
        assert isinstance(page, AuditPage)
        assert page.items == []
        assert page.total == 0

    def test_inherited_count_delegates_to_query(self):
        """count() is answered from query()'s total without the backend helping."""
        repo = StubAuditRepository()
        assert repo.count(AuditQuery(organization_id=1)) == 0

    def test_inherited_get_by_uid_delegates_to_query(self):
        """get_by_uid() is answered from query() without the backend helping."""
        repo = StubAuditRepository()
        assert repo.get_by_uid(uuid.uuid7()) is None

    def test_inherited_iter_records_terminates_on_empty_page(self):
        """iter_records() stops rather than looping when query() returns nothing."""
        repo = StubAuditRepository()
        assert list(repo.iter_records(AuditQuery(organization_id=1))) == []
