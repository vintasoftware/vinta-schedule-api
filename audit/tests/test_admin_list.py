"""Tests for the repository-backed AuditAdmin changelist.

Covers:
- Staff/superuser can load the audit changelist (HTTP 200).
- Non-staff users are redirected to login.
- Each filter narrows results via the repository:
  action, actor_type, created_at range, has_diff, organization.
- Pagination: page 2 shows the next slice; total reflects full count.
- Read-only enforcement:
  - has_add/change/delete_permission return False.
  - POST to add/change/delete admin URLs is rejected.
  - No add button appears in the changelist HTML.
- Backend-agnosticism: override container.audit_repository with a stub
  AuditRepository and confirm the changelist renders from the stub (proving
  no ORM dependency in the admin view itself).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.test import Client

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.factories import AuditFactory
from audit.repositories import AuditRepository
from audit.types import ActorSnapshot, AuditPage, AuditQuery, AuditRecord, SubjectRef
from organizations.models import Organization


User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def superuser(db):
    """Create a superuser for admin access."""
    return User.objects.create_superuser(
        email="admin@example.com",
        password="adminpassword",
    )


@pytest.fixture
def staff_user(db):
    """Create a staff (non-superuser) user for admin access."""
    return User.objects.create_user(
        email="staff@example.com",
        password="staffpassword",
        is_staff=True,
    )


@pytest.fixture
def admin_client(superuser):
    """Django test client logged in as a superuser."""
    c = Client()
    c.force_login(superuser)
    return c


@pytest.fixture
def staff_client(staff_user):
    """Django test client logged in as a staff user (not superuser)."""
    c = Client()
    c.force_login(staff_user)
    return c


@pytest.fixture
def anonymous_client():
    """Django test client with no session."""
    return Client()


CHANGELIST_URL = "/super/audit/audit/"


# ---------------------------------------------------------------------------
# Stub repository for backend-agnosticism tests
# ---------------------------------------------------------------------------


class StubAuditRepository(AuditRepository):
    """Minimal in-memory AuditRepository for testing admin backend-agnosticism.

    Accepts a fixed list of AuditRecord instances injected at construction time.
    query() returns a slice respecting offset/limit; no real filtering is applied.

    ``last_query`` records the most recent AuditQuery passed to query(), so
    tests can assert that _build_audit_query translated GET params correctly.
    """

    def __init__(self, records: list[AuditRecord]) -> None:
        self._records = records
        self.last_query: AuditQuery | None = None

    def add(self, data: Any) -> AuditRecord:  # type: ignore[override]
        raise NotImplementedError("StubAuditRepository is read-only")

    def get(self, audit_id: int) -> AuditRecord | None:
        for r in self._records:
            if r.id == audit_id:
                return r
        return None

    def query(
        self,
        q: AuditQuery,
        *,
        offset: int = 0,
        limit: int = 50,
        ordering: str = "-created_at",
    ) -> AuditPage:
        self.last_query = q
        page = self._records[offset : offset + limit]
        return AuditPage(items=page, total=len(self._records))


def _make_stub_record(record_id: int, org_id: int = 1) -> AuditRecord:
    """Build a minimal AuditRecord DTO for use in stub tests."""
    return AuditRecord(
        id=record_id,
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        organization_id=org_id,
        action=AuditAction.CREATE,
        actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
        subject=SubjectRef(subject_type="test.Model", subject_id=str(record_id)),
    )


# ---------------------------------------------------------------------------
# Basic access
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminAccess:
    """Basic access control for the audit changelist."""

    def test_superuser_can_load_changelist(self, admin_client: Client) -> None:
        """A superuser receives HTTP 200 for the audit changelist."""
        response = admin_client.get(CHANGELIST_URL)
        assert response.status_code == 200

    def test_staff_user_can_load_changelist(self, staff_client: Client) -> None:
        """A staff (non-superuser) user also gets 200 — staff permission is sufficient."""
        response = staff_client.get(CHANGELIST_URL)
        assert response.status_code == 200

    def test_anonymous_user_redirected_to_login(self, anonymous_client: Client) -> None:
        """Unauthenticated requests are redirected to the admin login page."""
        response = anonymous_client.get(CHANGELIST_URL)
        assert response.status_code == 302
        assert "/login/" in response["Location"]

    def test_changelist_page_contains_table(self, admin_client: Client) -> None:
        """The changelist renders a results table."""
        response = admin_client.get(CHANGELIST_URL)
        assert b"<table" in response.content

    def test_changelist_page_contains_filter_form(self, admin_client: Client) -> None:
        """The changelist renders a filter form."""
        response = admin_client.get(CHANGELIST_URL)
        assert b'id="audit-filters"' in response.content


# ---------------------------------------------------------------------------
# Filter tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminFilters:
    """Verify that filter params narrow the results returned by the repository."""

    def test_action_filter_narrows_results(self, admin_client: Client, db: Any) -> None:
        """Filtering by action=create shows the CREATE record and not the UPDATE record.

        Seed one CREATE and one UPDATE audit.  Filter by action=create.
        Assert the response renders exactly 1 matching row (the "Showing 1 of 1"
        summary) and that the UPDATE action does NOT appear in any table cell
        (i.e. is absent from <td> cells, not just from dropdown options).
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, action=AuditAction.CREATE)
        factory.create(org, action=AuditAction.UPDATE)

        response = admin_client.get(CHANGELIST_URL + "?action=create")
        assert response.status_code == 200
        content = response.content.decode()

        # Exactly 1 record should be returned — the CREATE one.
        assert "Showing 1 of 1 record" in content
        # The UPDATE action must not appear in a table data cell.
        assert "<td>update</td>" not in content

    def test_action_filter_excludes_non_matching(self, admin_client: Client, db: Any) -> None:
        """Records not matching the action filter are excluded from results."""
        org = baker.make(Organization)
        factory = AuditFactory()
        # Create only UPDATE records; then filter for DELETE — should see 0 rows.
        factory.create(org, action=AuditAction.UPDATE)
        factory.create(org, action=AuditAction.UPDATE)

        response = admin_client.get(CHANGELIST_URL + "?action=delete")
        assert response.status_code == 200
        assert b"No audit records found." in response.content

    def test_actor_type_filter_narrows_results(self, admin_client: Client, db: Any) -> None:
        """Filtering by actor_type=system shows the SYSTEM record, not the MEMBERSHIP one.

        Seed one SYSTEM audit and one MEMBERSHIP audit.  Filter by actor_type=system.
        Assert the response renders exactly 1 row and that "membership" does NOT
        appear in any table data cell (it can still appear in the dropdown option).
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, actor_type=AuditActorType.SYSTEM)
        factory.create(org, actor_type=AuditActorType.MEMBERSHIP, actor_id=99)

        response = admin_client.get(CHANGELIST_URL + "?actor_type=system")
        assert response.status_code == 200
        content = response.content.decode()

        # Exactly 1 record should be returned — the SYSTEM one.
        assert "Showing 1 of 1 record" in content
        # The MEMBERSHIP actor type must not appear in a table data cell.
        assert "<td>membership</td>" not in content

    def test_actor_type_filter_excludes_non_matching(self, admin_client: Client, db: Any) -> None:
        """Records not matching the actor_type filter are excluded."""
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, actor_type=AuditActorType.SYSTEM)

        response = admin_client.get(CHANGELIST_URL + "?actor_type=membership")
        assert response.status_code == 200
        assert b"No audit records found." in response.content

    def test_created_after_filter_narrows_results(self, admin_client: Client, db: Any) -> None:
        """Filtering by created_after excludes older records.

        Creates 2 records, backdates one to 2020, then asserts that filtering
        by created_after=2022 shows exactly 1 record (the recent one).
        """
        from audit.models import Audit

        org = baker.make(Organization)
        factory = AuditFactory()
        old_record = factory.create(org)

        # Force a past created_at on the old_record via direct DB update (auto_now_add).
        Audit.original_manager.filter(pk=old_record.pk).update(
            created_at=datetime(2020, 1, 1, tzinfo=UTC)
        )
        factory.create(org)
        # second record has auto_now_add = now (well after 2023-01-01).

        response = admin_client.get(CHANGELIST_URL + "?created_after=2022-01-01")
        assert response.status_code == 200
        content = response.content.decode()
        # Only the recent record should show; old one (2020) excluded.
        assert "Showing 1 of 1 record" in content
        # The backdated 2020 date should not appear in results.
        assert "2020-01-01" not in content

    def test_created_before_filter_narrows_results(self, admin_client: Client, db: Any) -> None:
        """Filtering by created_before excludes newer records.

        Creates 2 records, backdates one to 2020, then asserts that filtering
        by created_before=2021 shows exactly 1 record (the old one).
        """
        from audit.models import Audit

        org = baker.make(Organization)
        factory = AuditFactory()
        old_record = factory.create(org)
        Audit.original_manager.filter(pk=old_record.pk).update(
            created_at=datetime(2020, 6, 1, tzinfo=UTC)
        )
        factory.create(org)
        # second record has auto_now_add = now (2025+), well after 2021-01-01.

        response = admin_client.get(CHANGELIST_URL + "?created_before=2021-01-01")
        assert response.status_code == 200
        content = response.content.decode()
        # Only the old record (2020) should show; the recent one excluded.
        assert "Showing 1 of 1 record" in content
        assert "2020-06-01" in content  # the backdated record's date in the table

    def test_has_diff_yes_filter(self, admin_client: Client, db: Any) -> None:
        """Filtering has_diff=yes shows only records that have a diff.

        Creates one record with a diff and one without; asserts the filter
        returns exactly 1 record (with diff) and shows "Yes" in the diff column.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, diff={"field": {"old": "a", "new": "b"}})
        factory.create(org, diff=None)

        response = admin_client.get(CHANGELIST_URL + "?has_diff=yes")
        assert response.status_code == 200
        content = response.content.decode()
        # Exactly 1 record has a diff.
        assert "Showing 1 of 1 record" in content
        # The diff column shows "Yes" for the matching record.
        assert "<td>Yes</td>" in content

    def test_has_diff_no_filter(self, admin_client: Client, db: Any) -> None:
        """Filtering has_diff=no shows only records without a diff.

        Creates one record with a diff and one without; asserts the filter
        returns exactly 1 record (without diff) and shows "No" in the diff column.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, diff={"field": {"old": "a", "new": "b"}})
        factory.create(org, diff=None)

        response = admin_client.get(CHANGELIST_URL + "?has_diff=no")
        assert response.status_code == 200
        content = response.content.decode()
        # Exactly 1 record has no diff.
        assert "Showing 1 of 1 record" in content
        assert "<td>No</td>" in content

    def test_organization_filter_narrows_results(self, admin_client: Client, db: Any) -> None:
        """Filtering by organization_id shows only records for that org.

        Creates records in two orgs; asserts that filtering by org1 shows
        exactly 1 record and the org1 id appears in the org column.
        """
        org1 = baker.make(Organization)
        org2 = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org1)
        factory.create(org2)

        response = admin_client.get(CHANGELIST_URL + f"?organization_id={org1.pk}")
        assert response.status_code == 200
        content = response.content.decode()
        # Only 1 record (for org1) should show.
        assert "Showing 1 of 1 record" in content
        # The org1 id appears in the organization column (as a <td>).
        assert f"<td>{org1.pk}</td>" in content


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminPagination:
    """Verify pagination of the audit changelist."""

    def test_page_2_shows_next_slice(self, admin_client: Client, db: Any) -> None:
        """Page 2 with per_page=2 shows a disjoint set of subject IDs from page 1.

        Seed 5 audits each with a distinct subject_id so we can identify which
        records appear on each page.  Assert:
        - Each page renders exactly 2 rows (per_page=2).
        - The subject_ids rendered on page 1 and page 2 are fully disjoint.
        """
        import re

        org = baker.make(Organization)
        factory = AuditFactory()
        # Use distinct subject_ids so we can track which record is on which page.
        subject_ids = [f"subj-{i}" for i in range(1, 6)]
        for sid in subject_ids:
            factory.create(org, subject_id=sid)

        response_p1 = admin_client.get(CHANGELIST_URL + "?page=1&per_page=2")
        response_p2 = admin_client.get(CHANGELIST_URL + "?page=2&per_page=2")

        assert response_p1.status_code == 200
        assert response_p2.status_code == 200

        content_p1 = response_p1.content.decode()
        content_p2 = response_p2.content.decode()

        # Extract subject IDs rendered in <td> cells on each page.
        # The subject ID column renders as <td>subj-N</td>.
        ids_p1 = set(re.findall(r"<td>(subj-\d+)</td>", content_p1))
        ids_p2 = set(re.findall(r"<td>(subj-\d+)</td>", content_p2))

        # Each page should have exactly 2 distinct subject IDs.
        assert len(ids_p1) == 2, f"Expected 2 ids on page 1, got: {ids_p1}"
        assert len(ids_p2) == 2, f"Expected 2 ids on page 2, got: {ids_p2}"

        # The two pages must show disjoint sets of records.
        assert ids_p1.isdisjoint(ids_p2), f"Pages overlap: p1={ids_p1}, p2={ids_p2}"

    def test_total_reflects_full_count(self, admin_client: Client, db: Any) -> None:
        """The 'total' count in the context reflects all matching records."""
        org = baker.make(Organization)
        factory = AuditFactory()
        # Create 7 records; load with per_page=3 so there is definitely more than 1 page.
        for _ in range(7):
            factory.create(org)

        response = admin_client.get(CHANGELIST_URL + "?per_page=3")
        assert response.status_code == 200
        content = response.content.decode()
        # The template renders "Showing N of 7 record(s). Page 1 of 3."
        assert "7 record" in content
        assert "Page 1 of 3" in content

    def test_pagination_prev_next_links_present(self, admin_client: Client, db: Any) -> None:
        """When multiple pages exist, prev/next navigation links are rendered."""
        org = baker.make(Organization)
        factory = AuditFactory()
        for _ in range(4):
            factory.create(org)

        # Load page 2 of 2 (per_page=2).
        response = admin_client.get(CHANGELIST_URL + "?page=2&per_page=2")
        assert response.status_code == 200
        content = response.content.decode()
        # Page 2 has a "Previous" link but no "Next" link as active.
        assert "Previous" in content

    def test_empty_page_shows_no_records_message(self, admin_client: Client) -> None:
        """When no audit records exist the template shows the empty message."""
        response = admin_client.get(CHANGELIST_URL)
        assert response.status_code == 200
        assert b"No audit records found." in response.content


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminReadOnly:
    """Verify that add / change / delete are fully blocked."""

    def test_has_add_permission_returns_false(self, admin_client: Client) -> None:
        """The 'Add audit' button must NOT appear in the changelist."""
        response = admin_client.get(CHANGELIST_URL)
        assert response.status_code == 200
        # Django renders "Add <model>" buttons only when has_add_permission returns True.
        # Check neither the URL nor text for an add button appears.
        assert b"/audit/audit/add/" not in response.content
        content = response.content.decode()
        assert "Add audit" not in content

    def test_post_to_add_url_is_rejected(self, admin_client: Client) -> None:
        """POST to the admin add URL must be rejected (403 or redirect, not 200)."""
        response = admin_client.post("/super/audit/audit/add/", {})
        # Django admin redirects to the changelist or returns 403 when add is disabled.
        assert response.status_code in (403, 302)

    def test_get_add_url_is_rejected(self, admin_client: Client) -> None:
        """GET to the admin add URL must be rejected when has_add_permission=False."""
        response = admin_client.get("/super/audit/audit/add/")
        assert response.status_code in (403, 302)

    def test_has_change_permission_returns_false(self, admin_client: Client, db: Any) -> None:
        """The change view does not expose an editable form.

        In Django 6, if ``has_change_permission`` returns False but
        ``has_view_permission`` is True (the default), Django renders the change
        page in a read-only "view" mode rather than raising a 403.  The admin
        object is viewable but no Save button is rendered.
        This test verifies no POST-able change form is shown (no Save input).
        """
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        response = admin_client.get(f"/super/audit/audit/{record.pk}/change/")
        # Django may return 200 (read-only view) or 403; both are acceptable.
        # The important invariant is that no "Save" form input is present.
        assert response.status_code in (200, 403, 302)
        if response.status_code == 200:
            content = response.content.decode()
            # No save button present — the view is read-only.
            assert 'name="_save"' not in content

    def test_has_delete_permission_returns_false(self, admin_client: Client, db: Any) -> None:
        """Attempting to GET the delete view for a real record must be rejected."""
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        response = admin_client.get(f"/super/audit/audit/{record.pk}/delete/")
        assert response.status_code in (403, 302)

    def test_post_to_change_url_is_rejected(self, admin_client: Client, db: Any) -> None:
        """POST to the admin change URL must be rejected and the record must be unchanged."""
        from audit.models import Audit

        org = baker.make(Organization)
        record = AuditFactory().create(org)
        original_action = record.action

        response = admin_client.post(f"/super/audit/audit/{record.pk}/change/", {})
        assert response.status_code in (403, 302)

        # The record must still exist and be unmodified.
        unchanged = Audit.original_manager.get(pk=record.pk)
        assert unchanged.action == original_action

    def test_post_to_delete_url_is_rejected(self, admin_client: Client, db: Any) -> None:
        """POST to the admin delete URL must be rejected and the record must still exist."""
        from audit.models import Audit

        org = baker.make(Organization)
        record = AuditFactory().create(org)

        response = admin_client.post(f"/super/audit/audit/{record.pk}/delete/", {"post": "yes"})
        assert response.status_code in (403, 302)

        # The record must still exist in the database.
        assert Audit.original_manager.filter(pk=record.pk).exists()


# ---------------------------------------------------------------------------
# Backend-agnosticism: stub repository
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminBackendAgnosticism:
    """Verify the admin works when the ORM repository is replaced with a stub.

    This proves the changelist view reads exclusively from AuditRepository.query
    and has no hidden ORM dependency in the view layer itself.
    """

    def test_stub_repository_renders_changelist(self, admin_client: Client) -> None:
        """Overriding the repository with a stub returns its canned records."""
        stub_records = [_make_stub_record(i, org_id=1) for i in range(1, 4)]
        stub = StubAuditRepository(stub_records)

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        # Override the repository provider with the stub.
        with container.audit_repository.override(stub):
            response = admin_client.get(CHANGELIST_URL)

        assert response.status_code == 200
        content = response.content.decode()
        # The stub returns 3 records; verify the count appears in the page.
        assert "3 record" in content

    def test_stub_repository_filters_are_passed_through(self, admin_client: Client) -> None:
        """When filters are applied, the stub still receives the query and returns its records.

        This confirms the view doesn't bypass the repository based on filter values.
        """
        stub_record = _make_stub_record(42, org_id=99)
        stub = StubAuditRepository([stub_record])

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        with container.audit_repository.override(stub):
            response = admin_client.get(
                CHANGELIST_URL + "?action=create&actor_type=system&organization_id=99"
            )

        assert response.status_code == 200
        content = response.content.decode()
        # The stub always returns its fixed list regardless of filters, proving
        # the admin rendered from the stub rather than hitting the ORM.
        assert "1 record" in content
        assert "42" in content  # subject_id == "42"

    def test_stub_repository_pagination_uses_stub_total(self, admin_client: Client) -> None:
        """Pagination totals come from the stub, not the ORM."""
        stub_records = [_make_stub_record(i) for i in range(1, 11)]  # 10 stubs
        stub = StubAuditRepository(stub_records)

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        with container.audit_repository.override(stub):
            response = admin_client.get(CHANGELIST_URL + "?per_page=3")

        assert response.status_code == 200
        content = response.content.decode()
        assert "10 record" in content
        # 10 records / 3 per page = 4 pages
        assert "Page 1 of 4" in content

    def test_get_params_translated_to_audit_query(self, admin_client: Client) -> None:
        """GET filter params are correctly translated into AuditQuery fields.

        Issue a request with ?organization_id=99&action=create&actor_type=system
        and assert that the AuditQuery received by the stub repository has the
        correct field values.  This proves _build_audit_query populates AuditQuery
        correctly end-to-end through the view.
        """
        stub = StubAuditRepository([])

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        with container.audit_repository.override(stub):
            response = admin_client.get(
                CHANGELIST_URL + "?organization_id=99&action=create&actor_type=system"
            )

        assert response.status_code == 200
        assert stub.last_query is not None, "Repository.query() was never called"
        assert stub.last_query.organization_id == 99
        assert stub.last_query.actions == ["create"]
        assert stub.last_query.actor_type == "system"

    def test_orm_not_used_when_stub_overrides(self, admin_client: Client, db: Any) -> None:
        """With the stub active, real ORM audit records are NOT visible in the admin.

        This is the definitive proof of backend-agnosticism: if the view were
        using the ORM directly, it would return real DB records regardless of the
        stub injection.

        Strategy: create a real ORM record with a distinctive subject_type that the
        stub does NOT return; then assert that subject_type is absent from the rendered
        page (because the view used the stub, not the ORM).  The stub returns a record
        with a different distinctive subject_type.
        """
        # Distinctive subject_type strings unlikely to appear elsewhere in the HTML.
        real_subject_type = "audit_agnosticism_test.RealORMRecord"
        stub_subject_type = "audit_agnosticism_test.StubRecord"

        # Create a real ORM audit record with a distinctive subject_type.
        org = baker.make(Organization)
        AuditFactory().create(org, subject_type=real_subject_type)

        # Stub returns one record with a DIFFERENT subject_type.
        stub_record = AuditRecord(
            id=999,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            organization_id=org.pk,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=SubjectRef(subject_type=stub_subject_type, subject_id="stub-id"),
        )
        stub = StubAuditRepository([stub_record])

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        with container.audit_repository.override(stub):
            response = admin_client.get(CHANGELIST_URL)

        content = response.content.decode()
        # The real ORM subject_type must NOT appear — the admin used the stub.
        assert real_subject_type not in content
        # The stub's subject_type MUST appear — proving data came from the stub.
        assert stub_subject_type in content
