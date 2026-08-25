"""AuditService across a main repository plus additional ones.

Covers the three things the multi-repository arrangement promises:

* **Writes go to the main repository, then tentatively to the rest.** A replica
  that fails must not fail — or roll back — the write that succeeded.
* **Reads pick their repository by alias**, and the same AuditQuery means the
  same thing wherever it is pointed.
* **Sync repairs whatever replication lost**, is safe to re-run, and cannot
  duplicate a record it has already copied.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from unittest.mock import MagicMock

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.exceptions import UnknownAuditRepositoryError
from audit.repositories import DjangoORMAuditRepository, InMemoryAuditRepository
from audit.services import MAIN_REPOSITORY_ALIAS, AuditService
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
def archive() -> InMemoryAuditRepository:
    return InMemoryAuditRepository()


@pytest.fixture
def service(main, warehouse, archive) -> AuditService:
    return AuditService(
        repository=main,
        additional_repositories={"warehouse": warehouse, "archive": archive},
    )


def make_data(
    organization: Organization,
    *,
    action: str = AuditAction.CREATE,
    subject_id: str = "1",
    created_at: dt.datetime | None = None,
    uid: uuid.UUID | None = None,
) -> AuditRecordData:
    return AuditRecordData(
        organization_id=organization.pk,
        action=action,
        actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
        subject=SubjectRef(subject_type="organizations.Organization", subject_id=subject_id),
        uid=uid or uuid.uuid7(),
        created_at=created_at or dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC),
    )


# ---------------------------------------------------------------------------
# Repository selection
# ---------------------------------------------------------------------------


class TestRepositorySelection:
    def test_none_resolves_to_the_main_repository(self, service, main):
        assert service.get_repository() is main

    def test_the_main_alias_resolves_to_the_main_repository(self, service, main):
        assert service.get_repository(MAIN_REPOSITORY_ALIAS) is main

    def test_an_alias_resolves_to_its_additional_repository(self, service, warehouse):
        assert service.get_repository("warehouse") is warehouse

    def test_an_unknown_alias_raises_rather_than_falling_back(self, service):
        """Answering a question about one store with another's data is worse than failing."""
        with pytest.raises(UnknownAuditRepositoryError) as exc_info:
            service.get_repository("nope")
        assert "nope" in str(exc_info.value)
        assert "warehouse" in str(exc_info.value)

    def test_aliases_lists_main_first(self, service):
        assert service.repository_aliases[0] == MAIN_REPOSITORY_ALIAS
        assert set(service.repository_aliases) == {MAIN_REPOSITORY_ALIAS, "warehouse", "archive"}

    def test_a_main_key_among_the_additional_repositories_is_dropped(self, main):
        """The alias belongs to the main repository; two claimants would be ambiguous."""
        impostor = InMemoryAuditRepository()
        service = AuditService(
            repository=main, additional_repositories={MAIN_REPOSITORY_ALIAS: impostor}
        )
        assert service.get_repository(MAIN_REPOSITORY_ALIAS) is main
        assert service.additional_repositories == {}

    def test_no_additional_repositories_is_the_default(self, main):
        service = AuditService(repository=main)
        assert service.additional_repositories == {}
        assert service.repository_aliases == (MAIN_REPOSITORY_ALIAS,)

    def test_the_supplied_mapping_is_copied(self, main, warehouse):
        """Mutating the caller's dict afterwards must not reconfigure the service."""
        supplied = {"warehouse": warehouse}
        service = AuditService(repository=main, additional_repositories=supplied)
        supplied["late"] = InMemoryAuditRepository()
        assert set(service.additional_repositories) == {"warehouse"}


# ---------------------------------------------------------------------------
# Write + replicate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPersistAndReplicate:
    def test_persist_writes_the_main_repository(self, service, main, organization):
        data = make_data(organization)
        record = service.persist(data)
        assert main.get_by_uid(data.uid) is not None
        assert record.uid == data.uid

    def test_persist_replicates_to_every_additional_repository(
        self, service, warehouse, archive, organization
    ):
        data = make_data(organization)
        service.persist(data)
        assert warehouse.get_by_uid(data.uid) is not None
        assert archive.get_by_uid(data.uid) is not None

    def test_every_copy_shares_the_uid_and_created_at(self, service, main, warehouse, organization):
        """The two fields that make copies comparable — and upserts possible."""
        emitted_at = dt.datetime(2026, 3, 4, 5, 6, tzinfo=dt.UTC)
        data = make_data(organization, created_at=emitted_at)
        service.persist(data)

        in_main = main.get_by_uid(data.uid)
        in_warehouse = warehouse.get_by_uid(data.uid)
        assert in_main is not None and in_warehouse is not None
        assert in_main.uid == in_warehouse.uid == data.uid
        assert in_main.created_at == in_warehouse.created_at == emitted_at

    def test_a_failing_replica_does_not_fail_the_main_write(
        self, main, warehouse, organization, caplog
    ):
        """Replication is tentative: the record is already durable in main."""
        broken = MagicMock()
        broken.bulk_add.side_effect = RuntimeError("warehouse unreachable")
        service = AuditService(
            repository=main, additional_repositories={"broken": broken, "warehouse": warehouse}
        )
        data = make_data(organization)

        with caplog.at_level(logging.ERROR, logger="audit.services"):
            service.persist(data)

        assert main.get_by_uid(data.uid) is not None
        assert any("Failed to replicate" in r.message for r in caplog.records)

    def test_one_failing_replica_does_not_stop_the_others(self, main, warehouse, organization):
        broken = MagicMock()
        broken.bulk_add.side_effect = RuntimeError("down")
        service = AuditService(
            repository=main, additional_repositories={"broken": broken, "warehouse": warehouse}
        )
        data = make_data(organization)

        service.persist(data)

        assert warehouse.get_by_uid(data.uid) is not None

    def test_replicate_reports_per_target_outcomes(self, main, warehouse, organization):
        broken = MagicMock()
        broken.bulk_add.side_effect = RuntimeError("down")
        service = AuditService(
            repository=main, additional_repositories={"broken": broken, "warehouse": warehouse}
        )
        record = main.add(make_data(organization))

        assert service.replicate(record) == {"broken": False, "warehouse": True}

    def test_replicate_can_target_a_subset(self, service, warehouse, archive, organization):
        record = service.get_repository().add(make_data(organization))
        service.replicate(record, targets=["warehouse"])
        assert warehouse.get_by_uid(record.uid) is not None
        assert archive.get_by_uid(record.uid) is None

    def test_replicate_reports_an_unknown_target_as_a_failure(self, service, organization):
        """One bad alias must not stop the targets that are fine."""
        record = service.get_repository().add(make_data(organization))
        assert service.replicate(record, targets=["nope"]) == {"nope": False}

    def test_persisting_twice_does_not_duplicate_anywhere(
        self, service, main, warehouse, organization
    ):
        """CELERY_TASK_ACKS_LATE means the task can genuinely run twice."""
        data = make_data(organization)
        service.persist(data)
        service.persist(data)

        q = AuditQuery(organization_id=organization.pk)
        assert main.count(q) == 1
        assert warehouse.count(q) == 1


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReadsSelectTheirRepository:
    def test_query_defaults_to_main(self, service, main, warehouse, organization):
        main.add(make_data(organization, subject_id="only-in-main"))
        warehouse.add(make_data(organization, subject_id="only-in-warehouse"))

        page = service.query(AuditQuery(organization_id=organization.pk), limit=10)
        assert [r.subject.subject_id for r in page.items] == ["only-in-main"]

    def test_query_reads_the_named_repository(self, service, main, warehouse, organization):
        main.add(make_data(organization, subject_id="only-in-main"))
        warehouse.add(make_data(organization, subject_id="only-in-warehouse"))

        page = service.query(
            AuditQuery(organization_id=organization.pk), limit=10, repository="warehouse"
        )
        assert [r.subject.subject_id for r in page.items] == ["only-in-warehouse"]

    def test_get_by_uid_finds_the_same_record_in_either_repository(self, service, organization):
        data = make_data(organization)
        service.persist(data)

        from_main = service.get_by_uid(data.uid)
        from_warehouse = service.get_by_uid(data.uid, repository="warehouse")
        assert from_main is not None and from_warehouse is not None
        assert from_main.uid == from_warehouse.uid

    def test_count_compares_drift_between_repositories(
        self, service, main, warehouse, organization
    ):
        """The cheap way to see how far a replica has fallen behind."""
        for _ in range(3):
            main.add(make_data(organization))
        warehouse.add(make_data(organization))

        q = AuditQuery(organization_id=organization.pk)
        assert service.count(q) == 3
        assert service.count(q, repository="warehouse") == 1

    def test_iter_records_streams_the_named_repository(self, service, warehouse, organization):
        for i in range(4):
            warehouse.add(make_data(organization, subject_id=str(i)))

        streamed = list(
            service.iter_records(
                AuditQuery(organization_id=organization.pk),
                chunk_size=2,
                repository="warehouse",
            )
        )
        assert len(streamed) == 4

    @pytest.mark.parametrize("method", ["query", "count", "get_by_uid", "get"])
    def test_an_unknown_repository_raises_on_every_read(self, service, method):
        argument = {"query": AuditQuery(), "count": AuditQuery(), "get_by_uid": uuid.uuid7()}
        with pytest.raises(UnknownAuditRepositoryError):
            getattr(service, method)(argument.get(method, 1), repository="nope")


# ---------------------------------------------------------------------------
# Sync / backfill
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSyncRepository:
    def test_backfills_everything_the_target_is_missing(
        self, service, main, warehouse, organization
    ):
        records = [make_data(organization, subject_id=str(i)) for i in range(5)]
        main.bulk_add(records)

        result = service.sync_repository("warehouse")

        assert result.read == 5
        assert result.written == 5
        assert result.failed == 0
        assert result.ok
        assert warehouse.count(AuditQuery(organization_id=organization.pk)) == 5

    def test_the_copies_are_identical(self, service, main, warehouse, organization):
        data = make_data(organization, created_at=dt.datetime(2026, 2, 3, tzinfo=dt.UTC))
        main.add(data)

        service.sync_repository("warehouse")

        original = main.get_by_uid(data.uid)
        copy = warehouse.get_by_uid(data.uid)
        assert original is not None and copy is not None
        assert (original.uid, original.created_at, original.action) == (
            copy.uid,
            copy.created_at,
            copy.action,
        )

    def test_re_running_a_sync_does_not_duplicate(self, service, main, warehouse, organization):
        """Every write is an upsert on uid, so a completed sync can be repeated."""
        main.bulk_add([make_data(organization, subject_id=str(i)) for i in range(5)])

        service.sync_repository("warehouse")
        second = service.sync_repository("warehouse")

        assert second.written == 5
        assert warehouse.count(AuditQuery(organization_id=organization.pk)) == 5

    def test_syncing_over_records_replication_already_delivered_is_safe(
        self, service, main, warehouse, organization
    ):
        """The realistic case: live replication and a repair sync overlapping."""
        service.persist(make_data(organization, subject_id="replicated"))
        main.add(make_data(organization, subject_id="missed"))

        service.sync_repository("warehouse")

        assert warehouse.count(AuditQuery(organization_id=organization.pk)) == 2

    def test_a_query_narrows_the_window(self, service, main, warehouse, organization):
        boundary = dt.datetime(2026, 6, 10, tzinfo=dt.UTC)
        main.bulk_add(
            [
                make_data(
                    organization, subject_id="old", created_at=boundary - dt.timedelta(days=1)
                ),
                make_data(organization, subject_id="new", created_at=boundary),
            ]
        )

        result = service.sync_repository("warehouse", query=AuditQuery(created_after=boundary))

        assert result.read == 1
        page = warehouse.query(AuditQuery(), limit=10)
        assert [r.subject.subject_id for r in page.items] == ["new"]

    def test_a_query_narrows_to_one_tenant(self, service, main, warehouse, organization):
        other = baker.make(Organization)
        main.bulk_add([make_data(organization), make_data(other)])

        service.sync_repository("warehouse", query=AuditQuery(organization_id=organization.pk))

        assert warehouse.count(AuditQuery()) == 1
        assert warehouse.count(AuditQuery(organization_id=organization.pk)) == 1

    def test_batches_are_bounded(self, service, main, warehouse, organization):
        """A backfill must not need the whole log in memory."""
        main.bulk_add([make_data(organization, subject_id=str(i)) for i in range(7)])
        spy = MagicMock(wraps=warehouse.bulk_add)
        warehouse.bulk_add = spy

        service.sync_repository("warehouse", batch_size=3)

        assert [len(call.args[0]) for call in spy.call_args_list] == [3, 3, 1]

    def test_a_failing_batch_is_recorded_and_the_walk_continues(
        self, service, main, warehouse, organization, caplog
    ):
        """One bad chunk must not strand every record behind it."""
        main.bulk_add([make_data(organization, subject_id=str(i)) for i in range(6)])
        real_bulk_add = warehouse.bulk_add
        calls = {"n": 0}

        def flaky(batch):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient warehouse error")
            return real_bulk_add(batch)

        warehouse.bulk_add = flaky

        with caplog.at_level(logging.ERROR, logger="audit.services"):
            result = service.sync_repository("warehouse", batch_size=2)

        assert result.read == 6
        assert result.failed == 2
        assert result.written == 4
        assert not result.ok
        assert result.errors and "transient warehouse error" in result.errors[0]
        assert warehouse.count(AuditQuery(organization_id=organization.pk)) == 4

    def test_syncing_a_repository_from_itself_is_refused(self, service):
        """Rewriting a log with itself is never what the caller meant."""
        with pytest.raises(ValueError, match="from itself"):
            service.sync_repository("warehouse", source="warehouse")

    def test_an_unknown_target_raises(self, service):
        with pytest.raises(UnknownAuditRepositoryError):
            service.sync_repository("nope")

    def test_an_unknown_source_raises(self, service):
        with pytest.raises(UnknownAuditRepositoryError):
            service.sync_repository("warehouse", source="nope")

    def test_can_sync_between_two_additional_repositories(
        self, service, warehouse, archive, organization
    ):
        warehouse.bulk_add([make_data(organization) for _ in range(3)])

        result = service.sync_repository("archive", source="warehouse")

        assert result.source == "warehouse"
        assert result.target == "archive"
        assert archive.count(AuditQuery(organization_id=organization.pk)) == 3

    def test_syncing_an_empty_log_reports_nothing_done(self, service):
        result = service.sync_repository("warehouse")
        assert (result.read, result.written, result.failed) == (0, 0, 0)
        assert result.ok


@pytest.mark.django_db
class TestSyncAllRepositories:
    def test_backfills_every_additional_repository(
        self, service, main, warehouse, archive, organization
    ):
        main.bulk_add([make_data(organization) for _ in range(3)])

        results = service.sync_all_repositories()

        assert set(results) == {"warehouse", "archive"}
        assert all(r.ok for r in results.values())
        assert warehouse.count(AuditQuery()) == 3
        assert archive.count(AuditQuery()) == 3

    def test_skips_the_repository_it_is_reading_from(
        self, service, warehouse, archive, organization
    ):
        warehouse.bulk_add([make_data(organization) for _ in range(2)])

        results = service.sync_all_repositories(source="warehouse")

        assert set(results) == {"archive"}
        assert archive.count(AuditQuery()) == 2


@pytest.mark.django_db
class TestSyncAgainstTheORMRepository:
    """The realistic pairing: the ORM log as source, another backend as target."""

    def test_backfills_an_additional_repository_from_the_orm(self, organization):
        orm = DjangoORMAuditRepository()
        warehouse = InMemoryAuditRepository()
        service = AuditService(repository=orm, additional_repositories={"warehouse": warehouse})
        records = [make_data(organization, subject_id=str(i)) for i in range(4)]
        orm.bulk_add(records)

        result = service.sync_repository("warehouse", batch_size=2)

        assert result.read == 4
        assert warehouse.count(AuditQuery(organization_id=organization.pk)) == 4
        for data in records:
            copy = warehouse.get_by_uid(data.uid)
            assert copy is not None
            assert copy.created_at == data.created_at

    def test_a_record_written_before_the_replica_existed_is_backfilled(self, organization):
        """The reason a newly-added repository needs a backfill at all."""
        orm = DjangoORMAuditRepository()
        service_without_replica = AuditService(repository=orm)
        data = make_data(organization)
        service_without_replica.persist(data)

        warehouse = InMemoryAuditRepository()
        service_with_replica = AuditService(
            repository=orm, additional_repositories={"warehouse": warehouse}
        )
        service_with_replica.sync_repository("warehouse")

        assert warehouse.get_by_uid(data.uid) is not None
