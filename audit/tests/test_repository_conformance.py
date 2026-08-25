"""One filter suite, run against every AuditRepository implementation.

``AuditService`` lets a caller point the same ``AuditQuery`` at the main
repository or at any additional one, and the sync path reads from one
repository and writes into another. Both only work if the implementations
*agree* — same filter semantics, same upsert-on-uid behaviour, same ordering.
Testing each backend against its own expectations would not catch a drift
between them, so every test here is parametrized over both and asserts the
behaviour the interface promises rather than anything backend-specific.

``DjangoORMAuditRepository`` pushes the filters into SQL;
``InMemoryAuditRepository`` runs them through ``audit.filtering``. A new
backend joins by adding one line to ``REPOSITORY_FACTORIES``.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.repositories import DjangoORMAuditRepository, InMemoryAuditRepository
from audit.types import (
    ActorRef,
    ActorSnapshot,
    AuditQuery,
    AuditRecordData,
    SubjectKey,
    SubjectRef,
)
from organizations.models import Organization


REPOSITORY_FACTORIES = {
    "orm": DjangoORMAuditRepository,
    "memory": InMemoryAuditRepository,
}


@pytest.fixture(params=sorted(REPOSITORY_FACTORIES))
def repository(request):
    """Each test runs once per implementation."""
    return REPOSITORY_FACTORIES[request.param]()


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization)


def make_data(
    organization: Organization,
    *,
    action: str = AuditAction.CREATE,
    actor_type: str = AuditActorType.SYSTEM,
    actor_id: int | None = None,
    subject_type: str = "organizations.Organization",
    subject_id: str = "1",
    subject_label: str | None = None,
    affected_membership_ids: list[int] | None = None,
    diff: dict | None = None,
    created_at: dt.datetime | None = None,
    uid: uuid.UUID | None = None,
) -> AuditRecordData:
    """Build record data with everything defaulted except what a test cares about."""
    return AuditRecordData(
        organization_id=organization.pk,
        action=action,
        actor=ActorSnapshot(actor_type=actor_type, actor_id=actor_id),
        subject=SubjectRef(
            subject_type=subject_type, subject_id=subject_id, subject_label=subject_label
        ),
        affected_membership_ids=affected_membership_ids or [],
        diff=diff,
        uid=uid or uuid.uuid7(),
        created_at=created_at or dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC),
    )


@pytest.mark.django_db
class TestUpsertOnUid:
    """Writing the same record twice must converge, never duplicate.

    This is the property the whole replication + sync design rests on: a
    replica that receives a record it already has, a Celery task that runs
    twice, a backfill re-run over a window that is already in step.
    """

    def test_add_twice_with_the_same_uid_stores_one_record(self, repository, organization):
        data = make_data(organization, subject_label="first")
        repository.add(data)
        repository.add(data)

        page = repository.query(AuditQuery(organization_id=organization.pk), limit=100)
        assert page.total == 1

    def test_re_adding_keeps_the_repositorys_own_id_stable(self, repository, organization):
        data = make_data(organization)
        first = repository.add(data)
        second = repository.add(data)

        assert second.id == first.id
        assert second.uid == first.uid

    def test_re_adding_updates_the_payload(self, repository, organization):
        """A converging write rewrites content rather than being ignored."""
        uid = uuid.uuid7()
        repository.add(make_data(organization, uid=uid, subject_label="before"))
        repository.add(make_data(organization, uid=uid, subject_label="after"))

        stored = repository.get_by_uid(uid)
        assert stored is not None
        assert stored.subject.subject_label == "after"

    def test_created_at_is_the_value_supplied_not_the_write_time(self, repository, organization):
        """A replica must carry the ORIGINAL emit time, not its own clock.

        Without this, two copies of a record never compare equal and the
        created_at windows a sync runs under do not line up between backends.
        """
        emitted_at = dt.datetime(2020, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
        record = repository.add(make_data(organization, created_at=emitted_at))
        assert record.created_at == emitted_at

    def test_bulk_add_returns_records_in_input_order(self, repository, organization):
        data = [make_data(organization, subject_id=str(i)) for i in range(5)]
        records = repository.bulk_add(data)
        assert [r.subject.subject_id for r in records] == ["0", "1", "2", "3", "4"]
        assert [r.uid for r in records] == [d.uid for d in data]

    def test_bulk_add_of_an_empty_batch_is_a_no_op(self, repository, organization):
        assert repository.bulk_add([]) == []

    def test_bulk_add_over_an_overlapping_batch_does_not_duplicate(self, repository, organization):
        """The shape a sync actually produces: windows that overlap."""
        first_batch = [make_data(organization, subject_id=str(i)) for i in range(5)]
        repository.bulk_add(first_batch)
        # Re-send the tail of the first batch alongside new records.
        repository.bulk_add([*first_batch[3:], make_data(organization, subject_id="5")])

        assert repository.count(AuditQuery(organization_id=organization.pk)) == 6

    def test_affected_memberships_survive_a_re_add(self, repository, organization):
        uid = uuid.uuid7()
        data = make_data(organization, uid=uid, affected_membership_ids=[7, 9])
        repository.add(data)
        repository.add(data)

        stored = repository.get_by_uid(uid)
        assert stored is not None
        assert stored.affected_membership_ids == [7, 9]

    def test_duplicate_membership_ids_within_one_record_are_deduplicated(
        self, repository, organization
    ):
        record = repository.add(make_data(organization, affected_membership_ids=[7, 7, 9]))
        assert record.affected_membership_ids == [7, 9]


@pytest.mark.django_db
class TestIdentityLookup:
    """get_by_uid is the lookup that means the same thing in every backend."""

    def test_get_by_uid_finds_the_record(self, repository, organization):
        data = make_data(organization)
        repository.add(data)
        found = repository.get_by_uid(data.uid)
        assert found is not None
        assert found.uid == data.uid

    def test_get_by_uid_returns_none_when_absent(self, repository, organization):
        assert repository.get_by_uid(uuid.uuid7()) is None

    def test_get_uses_the_repositorys_own_id(self, repository, organization):
        record = repository.add(make_data(organization))
        found = repository.get(record.id)
        assert found is not None
        assert found.uid == record.uid


@pytest.mark.django_db
class TestFilterSemantics:
    """Every AuditQuery field, given the same meaning by every backend."""

    def test_none_means_the_filter_is_inactive(self, repository, organization):
        repository.bulk_add([make_data(organization) for _ in range(3)])
        assert repository.count(AuditQuery()) >= 3

    def test_empty_list_matches_nothing(self, repository, organization):
        """`[]` is an active, unsatisfiable filter — not an absent one.

        Code that builds a filter from a computed set must get an empty result
        rather than silently querying the whole log.
        """
        repository.bulk_add([make_data(organization) for _ in range(3)])
        assert repository.count(AuditQuery(organization_id=organization.pk, actions=[])) == 0
        assert repository.count(AuditQuery(organization_id=organization.pk, uids=[])) == 0
        assert repository.count(AuditQuery(organization_id=organization.pk, actors=[])) == 0
        assert repository.count(AuditQuery(organization_id=organization.pk, subjects=[])) == 0

    def test_actions_in(self, repository, organization):
        repository.bulk_add(
            [
                make_data(organization, action=AuditAction.CREATE),
                make_data(organization, action=AuditAction.UPDATE),
                make_data(organization, action=AuditAction.DELETE),
            ]
        )
        page = repository.query(
            AuditQuery(
                organization_id=organization.pk,
                actions=[AuditAction.CREATE, AuditAction.DELETE],
            ),
            limit=100,
        )
        assert {r.action for r in page.items} == {AuditAction.CREATE, AuditAction.DELETE}

    def test_actor_types_in(self, repository, organization):
        repository.bulk_add(
            [
                make_data(organization, actor_type=AuditActorType.SYSTEM),
                make_data(organization, actor_type=AuditActorType.MEMBERSHIP, actor_id=1),
                make_data(organization, actor_type=AuditActorType.SYSTEM_USER, actor_id=1),
            ]
        )
        page = repository.query(
            AuditQuery(
                organization_id=organization.pk,
                actor_types=[AuditActorType.MEMBERSHIP, AuditActorType.SYSTEM_USER],
            ),
            limit=100,
        )
        assert {r.actor.actor_type for r in page.items} == {
            AuditActorType.MEMBERSHIP,
            AuditActorType.SYSTEM_USER,
        }

    def test_actors_in_matches_on_the_type_and_id_pair(self, repository, organization):
        """An id is unique only within a type, so the pair has to travel together."""
        wanted = make_data(organization, actor_type=AuditActorType.MEMBERSHIP, actor_id=7)
        same_id_other_type = make_data(
            organization, actor_type=AuditActorType.SYSTEM_USER, actor_id=7
        )
        repository.bulk_add([wanted, same_id_other_type])

        page = repository.query(
            AuditQuery(
                organization_id=organization.pk,
                actors=[ActorRef(AuditActorType.MEMBERSHIP, 7)],
            ),
            limit=100,
        )
        assert [r.uid for r in page.items] == [wanted.uid]

    def test_actors_in_matches_a_null_actor_id(self, repository, organization):
        """`ActorRef(type, None)` means the SYSTEM actor, which genuinely has no id."""
        system = make_data(organization, actor_type=AuditActorType.SYSTEM, actor_id=None)
        repository.bulk_add(
            [system, make_data(organization, actor_type=AuditActorType.MEMBERSHIP, actor_id=1)]
        )
        page = repository.query(
            AuditQuery(
                organization_id=organization.pk,
                actors=[ActorRef(AuditActorType.SYSTEM, None)],
            ),
            limit=100,
        )
        assert [r.uid for r in page.items] == [system.uid]

    def test_subject_types_in(self, repository, organization):
        repository.bulk_add(
            [
                make_data(organization, subject_type="organizations.Organization"),
                make_data(organization, subject_type="calendar_integration.CalendarEvent"),
                make_data(organization, subject_type="payments.Subscription"),
            ]
        )
        page = repository.query(
            AuditQuery(
                organization_id=organization.pk,
                subject_types=[
                    "calendar_integration.CalendarEvent",
                    "payments.Subscription",
                ],
            ),
            limit=100,
        )
        assert {r.subject.subject_type for r in page.items} == {
            "calendar_integration.CalendarEvent",
            "payments.Subscription",
        }

    def test_subjects_in_matches_on_the_type_and_id_pair(self, repository, organization):
        wanted = make_data(organization, subject_type="a.Model", subject_id="1")
        same_id_other_type = make_data(organization, subject_type="b.Model", subject_id="1")
        repository.bulk_add([wanted, same_id_other_type])

        page = repository.query(
            AuditQuery(organization_id=organization.pk, subjects=[SubjectKey("a.Model", "1")]),
            limit=100,
        )
        assert [r.uid for r in page.items] == [wanted.uid]

    def test_subjects_in_accepts_a_subject_refs_key(self, repository, organization):
        """`SubjectRef.key` drops the label, which is a snapshot and not identity."""
        data = make_data(organization, subject_id="42", subject_label="Some Label")
        repository.add(data)

        page = repository.query(
            AuditQuery(organization_id=organization.pk, subjects=[data.subject.key]),
            limit=100,
        )
        assert [r.uid for r in page.items] == [data.uid]

    def test_affected_membership_ids_in_matches_any_of_them(self, repository, organization):
        alice = make_data(organization, affected_membership_ids=[1])
        bob = make_data(organization, affected_membership_ids=[2])
        carol = make_data(organization, affected_membership_ids=[3])
        repository.bulk_add([alice, bob, carol])

        page = repository.query(
            AuditQuery(organization_id=organization.pk, affected_membership_ids=[1, 3]),
            limit=100,
        )
        assert {r.uid for r in page.items} == {alice.uid, carol.uid}

    def test_affected_membership_ids_does_not_duplicate_a_multi_match(
        self, repository, organization
    ):
        """A record affecting two of the named memberships is still ONE result.

        The ORM path joins the through table, so without a distinct() this comes
        back once per matching link.
        """
        both = make_data(organization, affected_membership_ids=[1, 2])
        repository.add(both)

        page = repository.query(
            AuditQuery(organization_id=organization.pk, affected_membership_ids=[1, 2]),
            limit=100,
        )
        assert [r.uid for r in page.items] == [both.uid]
        assert page.total == 1

    def test_uids_in(self, repository, organization):
        records = [make_data(organization) for _ in range(4)]
        repository.bulk_add(records)
        wanted = [records[0].uid, records[2].uid]

        page = repository.query(AuditQuery(uids=wanted), limit=100)
        assert {r.uid for r in page.items} == set(wanted)

    def test_created_range_is_half_open(self, repository, organization):
        """Lower bound inclusive, upper exclusive — so sync windows tile."""
        boundary = dt.datetime(2026, 6, 10, tzinfo=dt.UTC)
        before = make_data(organization, created_at=boundary - dt.timedelta(seconds=1))
        on_boundary = make_data(organization, created_at=boundary)
        after = make_data(organization, created_at=boundary + dt.timedelta(seconds=1))
        repository.bulk_add([before, on_boundary, after])

        lower = repository.query(
            AuditQuery(organization_id=organization.pk, created_after=boundary), limit=100
        )
        assert {r.uid for r in lower.items} == {on_boundary.uid, after.uid}

        upper = repository.query(
            AuditQuery(organization_id=organization.pk, created_before=boundary), limit=100
        )
        assert {r.uid for r in upper.items} == {before.uid}

    def test_consecutive_windows_cover_every_record_exactly_once(self, repository, organization):
        """The property the half-open range exists for."""
        boundary = dt.datetime(2026, 6, 10, tzinfo=dt.UTC)
        records = [
            make_data(organization, created_at=boundary + dt.timedelta(hours=offset))
            for offset in (-2, -1, 0, 1, 2)
        ]
        repository.bulk_add(records)

        first = repository.query(
            AuditQuery(organization_id=organization.pk, created_before=boundary), limit=100
        )
        second = repository.query(
            AuditQuery(organization_id=organization.pk, created_after=boundary), limit=100
        )
        seen = [r.uid for r in first.items] + [r.uid for r in second.items]
        assert sorted(map(str, seen)) == sorted(str(r.uid) for r in records)

    def test_has_diff(self, repository, organization):
        with_diff = make_data(organization, diff={"name": {"old": "a", "new": "b"}})
        without_diff = make_data(organization, diff=None)
        repository.bulk_add([with_diff, without_diff])

        yes = repository.query(
            AuditQuery(organization_id=organization.pk, has_diff=True), limit=100
        )
        no = repository.query(
            AuditQuery(organization_id=organization.pk, has_diff=False), limit=100
        )
        assert [r.uid for r in yes.items] == [with_diff.uid]
        assert [r.uid for r in no.items] == [without_diff.uid]

    def test_empty_diff_counts_as_no_diff(self, repository, organization):
        """`{}` means "no changes" and is normalized to None at write time."""
        record = repository.add(make_data(organization, diff={}))
        assert record.diff is None
        page = repository.query(
            AuditQuery(organization_id=organization.pk, has_diff=False), limit=100
        )
        assert record.uid in {r.uid for r in page.items}

    def test_search_matches_subject_columns_case_insensitively(self, repository, organization):
        wanted = make_data(organization, subject_label="Quarterly Review")
        repository.bulk_add([wanted, make_data(organization, subject_label="Standup")])

        page = repository.query(
            AuditQuery(organization_id=organization.pk, search="quarterly"), limit=100
        )
        assert [r.uid for r in page.items] == [wanted.uid]

    def test_search_matches_actor_id_only_for_a_numeric_term(self, repository, organization):
        """A non-numeric term must skip the integer column, not raise."""
        wanted = make_data(organization, actor_type=AuditActorType.MEMBERSHIP, actor_id=4242)
        repository.bulk_add([wanted, make_data(organization)])

        numeric = repository.query(
            AuditQuery(organization_id=organization.pk, search="4242"), limit=100
        )
        assert wanted.uid in {r.uid for r in numeric.items}

        # Must not raise on a term that cannot be an integer.
        repository.query(
            AuditQuery(organization_id=organization.pk, search="not-a-number"), limit=100
        )

    def test_filters_and_together(self, repository, organization):
        wanted = make_data(
            organization, action=AuditAction.UPDATE, actor_type=AuditActorType.SYSTEM
        )
        repository.bulk_add(
            [
                wanted,
                make_data(
                    organization,
                    action=AuditAction.UPDATE,
                    actor_type=AuditActorType.MEMBERSHIP,
                    actor_id=1,
                ),
                make_data(
                    organization, action=AuditAction.CREATE, actor_type=AuditActorType.SYSTEM
                ),
            ]
        )
        page = repository.query(
            AuditQuery(
                organization_id=organization.pk,
                actions=[AuditAction.UPDATE],
                actor_types=[AuditActorType.SYSTEM],
            ),
            limit=100,
        )
        assert [r.uid for r in page.items] == [wanted.uid]

    def test_organization_id_isolates_tenants(self, repository, organization):
        other = baker.make(Organization)
        mine = make_data(organization)
        theirs = make_data(other)
        repository.bulk_add([mine, theirs])

        page = repository.query(AuditQuery(organization_id=organization.pk), limit=100)
        assert [r.uid for r in page.items] == [mine.uid]


@pytest.mark.django_db
class TestPaginationAndOrdering:
    """Paging and ordering, identical across backends."""

    @pytest.fixture
    def records(self, repository, organization):
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        data = [
            make_data(organization, subject_id=str(i), created_at=base + dt.timedelta(days=i))
            for i in range(5)
        ]
        repository.bulk_add(data)
        return data

    def test_total_counts_matches_not_page_size(self, repository, organization, records):
        page = repository.query(AuditQuery(organization_id=organization.pk), limit=2)
        assert len(page.items) == 2
        assert page.total == 5

    def test_offset_skips(self, repository, organization, records):
        page = repository.query(
            AuditQuery(organization_id=organization.pk), offset=2, limit=10, ordering="created_at"
        )
        assert [r.subject.subject_id for r in page.items] == ["2", "3", "4"]

    def test_default_ordering_is_newest_first(self, repository, organization, records):
        page = repository.query(AuditQuery(organization_id=organization.pk), limit=10)
        assert [r.subject.subject_id for r in page.items] == ["4", "3", "2", "1", "0"]

    def test_ascending_ordering(self, repository, organization, records):
        page = repository.query(
            AuditQuery(organization_id=organization.pk), limit=10, ordering="created_at"
        )
        assert [r.subject.subject_id for r in page.items] == ["0", "1", "2", "3", "4"]

    def test_unknown_ordering_falls_back_rather_than_raising(
        self, repository, organization, records
    ):
        """A stale or hostile value in a URL must not reach the database."""
        page = repository.query(
            AuditQuery(organization_id=organization.pk),
            limit=10,
            ordering="; DROP TABLE audit_audit; --",
        )
        assert [r.subject.subject_id for r in page.items] == ["4", "3", "2", "1", "0"]

    def test_count_agrees_with_query_total(self, repository, organization, records):
        q = AuditQuery(organization_id=organization.pk)
        assert repository.count(q) == repository.query(q, limit=1).total


@pytest.mark.django_db
class TestIterRecords:
    """Streaming the whole log, in bounded memory and without losing a record."""

    def test_yields_every_match_oldest_first(self, repository, organization):
        base = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
        data = [
            make_data(organization, subject_id=str(i), created_at=base + dt.timedelta(days=i))
            for i in range(7)
        ]
        repository.bulk_add(data)

        streamed = list(
            repository.iter_records(AuditQuery(organization_id=organization.pk), chunk_size=2)
        )
        assert [r.subject.subject_id for r in streamed] == ["0", "1", "2", "3", "4", "5", "6"]

    def test_a_chunk_boundary_does_not_drop_or_repeat(self, repository, organization):
        """Exactly chunk_size records must not produce an extra empty round-trip bug."""
        data = [make_data(organization, subject_id=str(i)) for i in range(4)]
        repository.bulk_add(data)

        streamed = list(
            repository.iter_records(AuditQuery(organization_id=organization.pk), chunk_size=4)
        )
        assert len(streamed) == 4

    def test_records_sharing_a_timestamp_are_each_yielded_once(self, repository, organization):
        """The uid tiebreak: same-instant records must not reshuffle across pages.

        Ordering by created_at alone leaves ties in an undefined order, so an
        offset-paginated walk can hand back one record twice and skip another.
        """
        same_instant = dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC)
        data = [
            make_data(organization, subject_id=str(i), created_at=same_instant) for i in range(10)
        ]
        repository.bulk_add(data)

        streamed = list(
            repository.iter_records(AuditQuery(organization_id=organization.pk), chunk_size=3)
        )
        assert sorted(str(r.uid) for r in streamed) == sorted(str(d.uid) for d in data)

    def test_respects_the_filter(self, repository, organization):
        wanted = make_data(organization, action=AuditAction.DELETE)
        repository.bulk_add([wanted, make_data(organization, action=AuditAction.CREATE)])

        streamed = list(
            repository.iter_records(
                AuditQuery(organization_id=organization.pk, actions=[AuditAction.DELETE])
            )
        )
        assert [r.uid for r in streamed] == [wanted.uid]

    def test_empty_log_yields_nothing(self, repository, organization):
        assert list(repository.iter_records(AuditQuery(organization_id=organization.pk))) == []
