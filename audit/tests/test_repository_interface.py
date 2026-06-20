from __future__ import annotations

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

    def test_has_add_method(self):
        """AuditRepository has abstract add method."""
        assert hasattr(AuditRepository, "add")
        assert getattr(AuditRepository.add, "__isabstractmethod__", False)

    def test_has_get_method(self):
        """AuditRepository has abstract get method."""
        assert hasattr(AuditRepository, "get")
        assert getattr(AuditRepository.get, "__isabstractmethod__", False)

    def test_has_query_method(self):
        """AuditRepository has abstract query method."""
        assert hasattr(AuditRepository, "query")
        assert getattr(AuditRepository.query, "__isabstractmethod__", False)

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
            created_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=ZoneInfo("UTC")),
            organization_id=data.organization_id,
            action=data.action,
            actor=data.actor,
            subject=data.subject,
            affected_membership_ids=data.affected_membership_ids,
            diff=data.diff,
        )

    def get(self, audit_id: int) -> AuditRecord | None:
        """Stub: return None."""
        return None

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str = "-created_at",
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

    def test_stub_get_returns_none(self):
        """Stub.get returns None."""
        repo = StubAuditRepository()
        result = repo.get(999)
        assert result is None

    def test_stub_query_returns_empty_page(self):
        """Stub.query returns an empty AuditPage."""
        repo = StubAuditRepository()
        q = AuditQuery(organization_id=1)
        page = repo.query(q)
        assert isinstance(page, AuditPage)
        assert page.items == []
        assert page.total == 0
