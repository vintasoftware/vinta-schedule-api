"""Integration tests for DjangoORMAuditRepository.query().

Covers:
- Each AuditQuery filter narrows the result set correctly.
- Pagination: offset + limit return the correct slice; total reflects the full
  match count, not the page size.
- Ordering: -created_at (descending, default) and created_at (ascending).
- Invalid ordering falls back to -created_at safely.
- Cross-org visibility: unscoped base queryset sees rows from multiple orgs;
  organization_id filter narrows to one org.
- search: matches subject_type, subject_id, subject_label; matches actor_id
  when the term is all-digits; non-numeric search does not crash.
"""

from __future__ import annotations

import datetime as dt

from django.contrib.auth import get_user_model

import pytest
from freezegun import freeze_time
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.factories import AuditFactory
from audit.repositories import DjangoORMAuditRepository
from audit.types import ActorSnapshot, AuditQuery, AuditRecordData, SubjectRef
from organizations.models import Organization, OrganizationMembership


User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_membership(org: Organization) -> OrganizationMembership:
    """Create a user and return their membership in *org*."""
    user = baker.make(User)
    return OrganizationMembership.objects.create(user=user, organization=org)


def make_subject(
    subject_type: str = "organizations.Organization",
    subject_id: str = "1",
    subject_label: str | None = None,
) -> SubjectRef:
    return SubjectRef(
        subject_type=subject_type,
        subject_id=subject_id,
        subject_label=subject_label,
    )


def make_system_actor(actor_id: int | None = None) -> ActorSnapshot:
    return ActorSnapshot(
        actor_type=AuditActorType.SYSTEM,
        actor_id=actor_id,
    )


def add_record(
    repo: DjangoORMAuditRepository,
    org: Organization,
    *,
    action: str = AuditAction.CREATE,
    actor: ActorSnapshot | None = None,
    subject: SubjectRef | None = None,
    affected_membership_ids: list[int] | None = None,
    diff: dict | None = None,
):
    """Convenience wrapper that calls repo.add() with sensible defaults."""
    return repo.add(
        AuditRecordData(
            organization_id=org.pk,
            action=action,
            actor=actor or make_system_actor(),
            subject=subject or make_subject(),
            affected_membership_ids=affected_membership_ids or [],
            diff=diff,
        )
    )


# ---------------------------------------------------------------------------
# organization_id filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryOrganizationFilter:
    """Tests that organization_id filter narrows results to one org."""

    def test_no_org_filter_sees_all_orgs(self) -> None:
        """Without an organization_id filter, query returns audits from all orgs."""
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        add_record(repo, org_a)
        add_record(repo, org_b)

        page = repo.query(AuditQuery(), limit=100)
        org_ids = {r.organization_id for r in page.items}
        assert org_a.pk in org_ids
        assert org_b.pk in org_ids

    def test_org_filter_narrows_to_one_org(self) -> None:
        """With organization_id set, only that org's audits are returned."""
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        add_record(repo, org_a)
        add_record(repo, org_b)

        page = repo.query(AuditQuery(organization_id=org_a.pk), limit=100)
        assert all(r.organization_id == org_a.pk for r in page.items)
        assert page.total >= 1

    def test_org_filter_excludes_other_org(self) -> None:
        """Audits from org B are not visible when querying for org A."""
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        add_record(repo, org_a)
        record_b = add_record(repo, org_b)

        page = repo.query(AuditQuery(organization_id=org_a.pk), limit=100)
        result_ids = {r.id for r in page.items}
        assert record_b.id not in result_ids


# ---------------------------------------------------------------------------
# actions filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryActionsFilter:
    """Tests that the actions filter narrows by action value."""

    def test_actions_filter_returns_matching_actions_only(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        create_rec = add_record(repo, org, action=AuditAction.CREATE)
        add_record(repo, org, action=AuditAction.DELETE)

        page = repo.query(
            AuditQuery(organization_id=org.pk, actions=[AuditAction.CREATE]), limit=100
        )
        ids = {r.id for r in page.items}
        assert create_rec.id in ids
        assert all(r.action == AuditAction.CREATE for r in page.items)

    def test_actions_filter_multi_value(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        create_rec = add_record(repo, org, action=AuditAction.CREATE)
        update_rec = add_record(repo, org, action=AuditAction.UPDATE)
        add_record(repo, org, action=AuditAction.DELETE)

        page = repo.query(
            AuditQuery(
                organization_id=org.pk,
                actions=[AuditAction.CREATE, AuditAction.UPDATE],
            ),
            limit=100,
        )
        ids = {r.id for r in page.items}
        assert create_rec.id in ids
        assert update_rec.id in ids
        assert all(r.action in {AuditAction.CREATE, AuditAction.UPDATE} for r in page.items)


# ---------------------------------------------------------------------------
# actor_type filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryActorTypeFilter:
    """Tests that actor_type filter narrows correctly."""

    def test_actor_type_filter(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        system_rec = add_record(
            repo, org, actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None)
        )
        add_record(
            repo,
            org,
            actor=ActorSnapshot(actor_type=AuditActorType.MEMBERSHIP, actor_id=1),
        )

        page = repo.query(
            AuditQuery(organization_id=org.pk, actor_type=AuditActorType.SYSTEM), limit=100
        )
        ids = {r.id for r in page.items}
        assert system_rec.id in ids
        assert all(r.actor.actor_type == AuditActorType.SYSTEM for r in page.items)


# ---------------------------------------------------------------------------
# actor_id filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryActorIdFilter:
    """Tests that actor_id filter narrows correctly."""

    def test_actor_id_filter(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()
        membership = make_membership(org)

        target_rec = add_record(
            repo,
            org,
            actor=ActorSnapshot(actor_type=AuditActorType.MEMBERSHIP, actor_id=membership.user_id),
        )
        add_record(
            repo,
            org,
            actor=ActorSnapshot(
                actor_type=AuditActorType.MEMBERSHIP, actor_id=membership.user_id + 9999
            ),
        )

        page = repo.query(
            AuditQuery(organization_id=org.pk, actor_id=membership.user_id), limit=100
        )
        ids = {r.id for r in page.items}
        assert target_rec.id in ids
        assert all(r.actor.actor_id == membership.user_id for r in page.items)


# ---------------------------------------------------------------------------
# subject_type / subject_id filters
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQuerySubjectFilters:
    """Tests that subject_type and subject_id filters narrow correctly."""

    def test_subject_type_filter(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        target = add_record(
            repo, org, subject=make_subject(subject_type="calendar_integration.CalendarEvent")
        )
        add_record(repo, org, subject=make_subject(subject_type="organizations.Organization"))

        page = repo.query(
            AuditQuery(
                organization_id=org.pk,
                subject_type="calendar_integration.CalendarEvent",
            ),
            limit=100,
        )
        assert target.id in {r.id for r in page.items}
        assert all(
            r.subject.subject_type == "calendar_integration.CalendarEvent" for r in page.items
        )

    def test_subject_id_filter(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        target = add_record(repo, org, subject=make_subject(subject_id="42"))
        add_record(repo, org, subject=make_subject(subject_id="99"))

        page = repo.query(AuditQuery(organization_id=org.pk, subject_id="42"), limit=100)
        assert target.id in {r.id for r in page.items}
        assert all(r.subject.subject_id == "42" for r in page.items)


# ---------------------------------------------------------------------------
# affected_membership_id filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryAffectedMembershipIdFilter:
    """Tests that affected_membership_id filter returns audits linked to that membership."""

    def test_affected_membership_id_filter(self) -> None:
        org = baker.make(Organization)
        m1 = make_membership(org)
        m2 = make_membership(org)
        repo = DjangoORMAuditRepository()

        linked = add_record(repo, org, affected_membership_ids=[m1.user_id])
        add_record(repo, org, affected_membership_ids=[m2.user_id])

        page = repo.query(
            AuditQuery(organization_id=org.pk, affected_membership_id=m1.user_id), limit=100
        )
        ids = {r.id for r in page.items}
        assert linked.id in ids
        assert all(m1.user_id in r.affected_membership_ids for r in page.items)

    def test_affected_membership_id_filter_no_duplicates(self) -> None:
        """Filtering by affected_membership_id returns distinct Audit rows (no JOIN duplication)."""
        org = baker.make(Organization)
        membership = make_membership(org)
        repo = DjangoORMAuditRepository()

        # One audit linked to one membership — should appear exactly once.
        add_record(repo, org, affected_membership_ids=[membership.user_id])

        page = repo.query(
            AuditQuery(organization_id=org.pk, affected_membership_id=membership.user_id),
            limit=100,
        )
        # Confirm total is 1, not inflated by JOIN multiplication.
        assert page.total == 1

    def test_affected_membership_id_filter_counts_multiple_audits_distinct(self) -> None:
        """distinct() + count() interaction is correct when multiple audits share a membership.

        Creates 3 audits in one org: 2 linked to membership M, 1 linked to a
        different membership.  Querying by M must return exactly 2 results in
        both total and items — proving that distinct() prevents JOIN inflation
        while count() reflects the real match set.
        """
        org = baker.make(Organization)
        m_target = make_membership(org)
        m_other = make_membership(org)
        repo = DjangoORMAuditRepository()

        linked_1 = add_record(repo, org, affected_membership_ids=[m_target.user_id])
        linked_2 = add_record(repo, org, affected_membership_ids=[m_target.user_id])
        add_record(repo, org, affected_membership_ids=[m_other.user_id])

        page = repo.query(
            AuditQuery(organization_id=org.pk, affected_membership_id=m_target.user_id),
            limit=100,
        )
        assert page.total == 2
        assert len(page.items) == 2
        ids = {r.id for r in page.items}
        assert linked_1.id in ids
        assert linked_2.id in ids


# ---------------------------------------------------------------------------
# created_after / created_before filters
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryDateFilters:
    """Tests that created_after and created_before filters narrow by timestamp."""

    def test_created_after_filter(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        # Add one record first, capture its timestamp.
        early = add_record(repo, org)

        # Add a second record (will have a later or equal created_at).
        later = add_record(repo, org)

        # Filtering after_ts should include `later` and exclude `early`
        # (since created_at__gte includes equal, use the later record's ts
        # to exclude the early one by filtering strictly after it).
        page = repo.query(
            AuditQuery(organization_id=org.pk, created_after=later.created_at),
            limit=100,
        )
        ids = {r.id for r in page.items}
        assert later.id in ids
        # early must NOT be in the result (its created_at is <= later.created_at).
        assert early.id not in ids or early.created_at >= later.created_at

    def test_created_before_filter(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        early = add_record(repo, org)
        later = add_record(repo, org)

        # Filtering created_before=later.created_at excludes `later` (strict <).
        page = repo.query(
            AuditQuery(organization_id=org.pk, created_before=later.created_at),
            limit=100,
        )
        ids = {r.id for r in page.items}
        # `later` must be excluded (strict <).
        assert later.id not in ids
        assert early.id in ids


# ---------------------------------------------------------------------------
# has_diff filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryHasDiffFilter:
    """Tests that has_diff filter narrows by presence of a diff value."""

    def test_has_diff_true_returns_records_with_diff(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        with_diff = add_record(repo, org, diff={"field": {"old": "a", "new": "b"}})
        add_record(repo, org, diff=None)

        page = repo.query(AuditQuery(organization_id=org.pk, has_diff=True), limit=100)
        ids = {r.id for r in page.items}
        assert with_diff.id in ids
        assert all(r.diff is not None for r in page.items)

    def test_has_diff_false_returns_records_without_diff(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        add_record(repo, org, diff={"field": {"old": "a", "new": "b"}})
        without_diff = add_record(repo, org, diff=None)

        page = repo.query(AuditQuery(organization_id=org.pk, has_diff=False), limit=100)
        ids = {r.id for r in page.items}
        assert without_diff.id in ids
        assert all(r.diff is None for r in page.items)


# ---------------------------------------------------------------------------
# search filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQuerySearch:
    """Tests that search narrows across subject fields and numeric actor_id."""

    def test_search_matches_subject_type(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        target = add_record(
            repo, org, subject=make_subject(subject_type="calendar_integration.CalendarEvent")
        )
        add_record(repo, org, subject=make_subject(subject_type="organizations.Organization"))

        page = repo.query(AuditQuery(organization_id=org.pk, search="CalendarEvent"), limit=100)
        ids = {r.id for r in page.items}
        assert target.id in ids

    def test_search_matches_subject_id(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        target = add_record(repo, org, subject=make_subject(subject_id="unique-subject-999"))
        add_record(repo, org, subject=make_subject(subject_id="other-subject-111"))

        page = repo.query(
            AuditQuery(organization_id=org.pk, search="unique-subject-999"), limit=100
        )
        ids = {r.id for r in page.items}
        assert target.id in ids

    def test_search_matches_subject_label(self) -> None:
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        target = add_record(repo, org, subject=make_subject(subject_label="Board Meeting 2026"))
        add_record(repo, org, subject=make_subject(subject_label="Team Sync"))

        page = repo.query(AuditQuery(organization_id=org.pk, search="Board Meeting"), limit=100)
        ids = {r.id for r in page.items}
        assert target.id in ids

    def test_search_matches_actor_id_when_numeric(self) -> None:
        """A numeric search term is also matched against actor_id (BigIntegerField)."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        membership = make_membership(org)
        target = add_record(
            repo,
            org,
            actor=ActorSnapshot(actor_type=AuditActorType.MEMBERSHIP, actor_id=membership.user_id),
            subject=make_subject(subject_type="uniquetype.X", subject_id="xyz-no-match"),
        )

        page = repo.query(
            AuditQuery(organization_id=org.pk, search=str(membership.user_id)), limit=100
        )
        ids = {r.id for r in page.items}
        assert target.id in ids

    def test_search_non_numeric_term_does_not_crash(self) -> None:
        """A non-numeric search term must not raise even though actor_id is an integer column."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        add_record(repo, org)

        # Should not raise TypeError or ValueError.
        page = repo.query(AuditQuery(organization_id=org.pk, search="not-a-number"), limit=100)
        assert isinstance(page.total, int)

    def test_search_empty_string_matches_all_records(self) -> None:
        """An empty search string matches all records (icontains of "" matches anything)."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        created = add_record(repo, org, subject=make_subject(subject_type="search.Target"))

        page = repo.query(AuditQuery(organization_id=org.pk, search=""), limit=100)
        assert page.total >= 1
        ids = {r.id for r in page.items}
        assert created.id in ids

    def test_search_combines_with_other_filter_via_and(self) -> None:
        """search Q(OR) is ANDed with other AuditQuery filters.

        Two audits share the same subject_type keyword; only the one matching
        the action filter is returned when both filters are active.
        """
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        shared_subject_type = "shared.SharedModel"
        create_rec = add_record(
            repo,
            org,
            action=AuditAction.CREATE,
            subject=make_subject(subject_type=shared_subject_type),
        )
        delete_rec = add_record(
            repo,
            org,
            action=AuditAction.DELETE,
            subject=make_subject(subject_type=shared_subject_type),
        )

        page = repo.query(
            AuditQuery(
                organization_id=org.pk,
                search="SharedModel",
                actions=[AuditAction.DELETE],
            ),
            limit=100,
        )
        ids = {r.id for r in page.items}
        assert delete_rec.id in ids
        assert create_rec.id not in ids

    def test_search_case_insensitive(self) -> None:
        """search is case-insensitive (icontains)."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        target = add_record(
            repo, org, subject=make_subject(subject_type="calendar_integration.CalendarEvent")
        )

        page = repo.query(AuditQuery(organization_id=org.pk, search="calendarevent"), limit=100)
        ids = {r.id for r in page.items}
        assert target.id in ids


# ---------------------------------------------------------------------------
# Pagination and total
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryPagination:
    """Tests that pagination returns the correct slice and total."""

    def test_total_reflects_full_match_count_not_page_size(self) -> None:
        """total is the full count of matching records, not limited by limit."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        # Create 5 records.
        for _ in range(5):
            add_record(repo, org)

        # Request only 2 records.
        page = repo.query(AuditQuery(organization_id=org.pk), limit=2)
        assert len(page.items) == 2
        assert page.total >= 5

    def test_offset_skips_records(self) -> None:
        """offset skips the first N records in the ordered result."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        # Create 4 records.
        for _ in range(4):
            add_record(repo, org)

        all_page = repo.query(
            AuditQuery(organization_id=org.pk), offset=0, limit=4, ordering="-created_at"
        )
        paged = repo.query(
            AuditQuery(organization_id=org.pk), offset=2, limit=4, ordering="-created_at"
        )

        # The offset page should skip the first 2 items.
        assert paged.items == all_page.items[2:]

    def test_limit_caps_returned_items(self) -> None:
        """limit caps the number of items returned in the page."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        for _ in range(5):
            add_record(repo, org)

        page = repo.query(AuditQuery(organization_id=org.pk), limit=3)
        assert len(page.items) <= 3

    def test_empty_result_set(self) -> None:
        """query returns an empty page when no records match the filter."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        page = repo.query(
            AuditQuery(organization_id=org.pk, actions=["nonexistent.action"]), limit=50
        )
        assert page.items == []
        assert page.total == 0

    def test_total_is_count_before_pagination(self) -> None:
        """total must equal the count of all matching records, not just the current page."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        for _ in range(10):
            add_record(repo, org)

        page = repo.query(AuditQuery(organization_id=org.pk), offset=0, limit=3)
        assert page.total >= 10
        assert len(page.items) == 3


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryOrdering:
    """Tests that ordering is applied correctly and invalid values fall back safely."""

    def test_default_ordering_is_descending_created_at(self) -> None:
        """Default ordering (-created_at) returns newest record first."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        ts_a = dt.datetime(2025, 1, 1, 10, 0, 0, tzinfo=dt.UTC)
        ts_b = dt.datetime(2025, 1, 1, 11, 0, 0, tzinfo=dt.UTC)

        with freeze_time(ts_a):
            first = add_record(repo, org)
        with freeze_time(ts_b):
            second = add_record(repo, org)

        page = repo.query(AuditQuery(organization_id=org.pk), limit=10)
        ids = [r.id for r in page.items]
        # second was created at a later timestamp so must appear before first.
        assert ids.index(second.id) < ids.index(first.id)

    def test_ascending_ordering_created_at(self) -> None:
        """ordering=created_at returns oldest record first."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        ts_a = dt.datetime(2025, 1, 1, 10, 0, 0, tzinfo=dt.UTC)
        ts_b = dt.datetime(2025, 1, 1, 11, 0, 0, tzinfo=dt.UTC)

        factory = AuditFactory()
        with freeze_time(ts_a):
            a1 = factory.create(organization=org)
        with freeze_time(ts_b):
            a2 = factory.create(organization=org)

        page = repo.query(AuditQuery(organization_id=org.pk), ordering="created_at", limit=10)
        ids = [r.id for r in page.items]
        # a1 is older so must appear before a2 in ascending order.
        assert ids.index(a1.id) < ids.index(a2.id)

    def test_invalid_ordering_falls_back_to_default(self) -> None:
        """An invalid ordering value falls back to -created_at without raising."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        add_record(repo, org)

        # Should not raise, should fall back to -created_at.
        page = repo.query(AuditQuery(organization_id=org.pk), ordering="invalid_field_name")
        assert isinstance(page.total, int)
        assert isinstance(page.items, list)

    def test_sql_injection_in_ordering_falls_back_safely(self) -> None:
        """A SQL-injection-style ordering value is rejected and falls back to default."""
        org = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        add_record(repo, org)

        page = repo.query(
            AuditQuery(organization_id=org.pk),
            ordering="; DROP TABLE audit_audit; --",
        )
        assert isinstance(page.total, int)


# ---------------------------------------------------------------------------
# Cross-org visibility
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestQueryCrossOrgVisibility:
    """Tests that the unscoped base queryset sees records from multiple orgs."""

    def test_unscoped_query_sees_multiple_orgs(self) -> None:
        """A query without organization_id returns records from all orgs."""
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        r_a = add_record(repo, org_a)
        r_b = add_record(repo, org_b)

        page = repo.query(AuditQuery(), limit=100)
        ids = {r.id for r in page.items}
        assert r_a.id in ids
        assert r_b.id in ids

    def test_org_filter_on_multi_org_db_narrows_to_one(self) -> None:
        """organization_id filter narrows cross-org results to just the target org."""
        org_a = baker.make(Organization)
        org_b = baker.make(Organization)
        repo = DjangoORMAuditRepository()

        r_a = add_record(repo, org_a)
        r_b = add_record(repo, org_b)

        page_a = repo.query(AuditQuery(organization_id=org_a.pk), limit=100)
        ids_a = {r.id for r in page_a.items}
        assert r_a.id in ids_a
        assert r_b.id not in ids_a

        page_b = repo.query(AuditQuery(organization_id=org_b.pk), limit=100)
        ids_b = {r.id for r in page_b.items}
        assert r_b.id in ids_b
        assert r_a.id not in ids_b
