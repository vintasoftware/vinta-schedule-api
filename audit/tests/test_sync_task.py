"""Tests for the sync_audit_repository Celery task.

The operational entry point for reconciling a replica out of band. What matters
here is the task boundary: JSON-only arguments crossing the broker, and failures
that log rather than crash the worker.

The sync behaviour itself (idempotence, batching, windows) is proved in
test_service_multi_repository.py against the service.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.repositories import InMemoryAuditRepository
from audit.services import AuditService
from audit.tasks import sync_audit_repository
from audit.types import ActorSnapshot, AuditQuery, AuditRecordData, SubjectRef
from organizations.models import Organization


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization)


@pytest.fixture
def main() -> InMemoryAuditRepository:
    return InMemoryAuditRepository()


@pytest.fixture
def warehouse() -> InMemoryAuditRepository:
    return InMemoryAuditRepository()


@pytest.fixture
def service(main, warehouse) -> AuditService:
    return AuditService(repository=main, additional_repositories={"warehouse": warehouse})


def make_data(organization: Organization, *, created_at: dt.datetime) -> AuditRecordData:
    return AuditRecordData(
        organization_id=organization.pk,
        action=AuditAction.CREATE,
        actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
        subject=SubjectRef(subject_type="organizations.Organization", subject_id="1"),
        uid=uuid.uuid7(),
        created_at=created_at,
    )


@pytest.mark.django_db
class TestSyncAuditRepositoryTask:
    def test_backfills_the_target(self, service, main, warehouse, organization):
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        main.bulk_add([make_data(organization, created_at=base) for _ in range(3)])

        result = sync_audit_repository("warehouse", audit_service=service)

        assert result == {
            "source": "main",
            "target": "warehouse",
            "read": 3,
            "written": 3,
            "failed": 0,
            "errors": [],
        }
        assert warehouse.count(AuditQuery()) == 3

    def test_the_result_is_json_safe(self, service, main, organization):
        """It is a Celery task return value, so it has to survive the result backend."""
        import json

        main.add(make_data(organization, created_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC)))
        result = sync_audit_repository("warehouse", audit_service=service)
        assert json.loads(json.dumps(result)) == result

    def test_the_window_arrives_as_iso_strings(self, service, main, warehouse, organization):
        """CELERY_TASK_SERIALIZER is json, so datetimes cannot cross the broker."""
        boundary = dt.datetime(2026, 6, 10, tzinfo=dt.UTC)
        main.bulk_add(
            [
                make_data(organization, created_at=boundary - dt.timedelta(days=1)),
                make_data(organization, created_at=boundary),
            ]
        )

        result = sync_audit_repository(
            "warehouse", created_after=boundary.isoformat(), audit_service=service
        )

        assert result is not None
        assert result["read"] == 1

    def test_organization_id_narrows_to_one_tenant(self, service, main, warehouse, organization):
        other = baker.make(Organization)
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        main.bulk_add([make_data(organization, created_at=base), make_data(other, created_at=base)])

        sync_audit_repository("warehouse", organization_id=organization.pk, audit_service=service)

        assert warehouse.count(AuditQuery()) == 1

    def test_re_running_the_task_does_not_duplicate(self, service, main, warehouse, organization):
        """CELERY_TASK_ACKS_LATE — a backfill may re-run after a worker dies."""
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        main.bulk_add([make_data(organization, created_at=base) for _ in range(4)])

        sync_audit_repository("warehouse", audit_service=service)
        sync_audit_repository("warehouse", audit_service=service)

        assert warehouse.count(AuditQuery()) == 4

    def test_an_unparseable_window_logs_and_returns_none(self, service, caplog):
        with caplog.at_level(logging.ERROR, logger="audit.tasks"):
            result = sync_audit_repository(
                "warehouse", created_after="yesterday", audit_service=service
            )

        assert result is None
        assert any("unparseable window" in r.message for r in caplog.records)

    def test_an_unknown_target_logs_and_returns_none(self, service, caplog):
        """A bad alias must not crash the worker process."""
        with caplog.at_level(logging.ERROR, logger="audit.tasks"):
            result = sync_audit_repository("nope", audit_service=service)

        assert result is None
        assert any("failed to run" in r.message for r in caplog.records)

    def test_a_missing_service_logs_and_returns_none(self, caplog):
        with caplog.at_level(logging.ERROR, logger="audit.tasks"):
            result = sync_audit_repository("warehouse", audit_service=None)

        assert result is None
        assert any("audit_service is not injected" in r.message for r in caplog.records)

    def test_a_partly_failing_sync_reports_its_errors(self, service, main, organization):
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        main.bulk_add([make_data(organization, created_at=base) for _ in range(2)])
        service.additional_repositories["warehouse"].bulk_add = _raise

        result = sync_audit_repository("warehouse", audit_service=service)

        assert result is not None
        assert result["failed"] == 2
        assert result["errors"]


def _raise(batch):
    raise RuntimeError("warehouse unreachable")
