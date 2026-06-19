"""Tests for the audit admin search and affected-membership filtering.

Phase 7: Search and affected-membership-id filtering in the admin changelist.

Covers:
- Search by subject type/id/label via ?search=<term>
- Search by numeric actor id via ?search=<digit-only term>
- Filter by affected membership id via ?affected_membership_id=<int>
- Empty/invalid search terms (blank, garbage, non-numeric affected_id)
- Integration with the repository's search capability
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.test import Client

import pytest
from model_bakery import baker

from audit.constants import AuditActorType
from audit.factories import AuditFactory
from audit.models import AuditAffectedMembership
from audit.types import AuditPage, AuditQuery, AuditRecord
from organizations.models import Organization, OrganizationMembership


User = get_user_model()

CHANGELIST_URL = "/super/audit/audit/"


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubRepositoryCapture:
    """Stub AuditRepository that captures the last query for assertion."""

    def __init__(self):
        self.last_query: AuditQuery | None = None

    def add(self, data: Any) -> AuditRecord:  # type: ignore[override]
        raise NotImplementedError()

    def get(self, audit_id: int) -> AuditRecord | None:
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
        return AuditPage(items=[], total=0)


@pytest.fixture
def admin_client():
    """Django test client logged in as a superuser."""
    user = User.objects.create_superuser(
        email="admin@example.com",
        password="adminpassword",
    )
    c = Client()
    c.force_login(user)
    return c


# ---------------------------------------------------------------------------
# Search by subject
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminSearchBySubject:
    """Test searching by subject type, subject id, and subject label."""

    def test_search_by_subject_type(self, admin_client: Client, db: Any) -> None:
        """Searching for a subject_type substring returns only matching rows.

        Seed audits with distinct subject_type values; search for one and assert
        only the matching row appears.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, subject_type="calendar.Event")
        factory.create(org, subject_type="meeting.Room")

        response = admin_client.get(CHANGELIST_URL + "?search=calendar")
        assert response.status_code == 200
        content = response.content.decode()

        # Only the calendar.Event record should match.
        assert "Showing 1 of 1 record" in content
        assert "calendar.Event" in content
        assert "meeting.Room" not in content

    def test_search_by_subject_id(self, admin_client: Client, db: Any) -> None:
        """Searching for a subject_id substring returns only matching rows.

        Seed audits with distinct subject_id values; search for one and assert
        only the matching row appears.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, subject_id="event-12345")
        factory.create(org, subject_id="event-67890")

        response = admin_client.get(CHANGELIST_URL + "?search=12345")
        assert response.status_code == 200
        content = response.content.decode()

        # Only the record with subject_id containing 12345 should match.
        assert "Showing 1 of 1 record" in content
        assert "event-12345" in content
        assert "event-67890" not in content

    def test_search_by_subject_label(self, admin_client: Client, db: Any) -> None:
        """Searching for a subject_label substring returns only matching rows.

        Seed audits with distinct subject_label values; search for one.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, subject_label="Team Meeting Q1")
        factory.create(org, subject_label="Standup Monday")

        response = admin_client.get(CHANGELIST_URL + "?search=Team Meeting")
        assert response.status_code == 200
        content = response.content.decode()

        # Only the record with matching label should appear.
        assert "Showing 1 of 1 record" in content

    def test_search_by_subject_returns_no_match(self, admin_client: Client, db: Any) -> None:
        """Searching for a non-existent subject term returns zero rows without error.

        Seed a record and search for a term that doesn't match anything.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, subject_type="calendar.Event", subject_id="event-123")

        response = admin_client.get(CHANGELIST_URL + "?search=zzz-no-match")
        assert response.status_code == 200
        assert b"No audit records found." in response.content


# ---------------------------------------------------------------------------
# Search by actor id (numeric search)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminSearchByActorId:
    """Test searching by numeric actor_id."""

    def test_search_by_actor_id_numeric_term(self, admin_client: Client, db: Any) -> None:
        """Searching for a numeric term matches actor_id when the term is all digits.

        Seed an audit with actor_id=42 and another with actor_id=99.
        Search for "42" and assert only the first record appears.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, actor_type=AuditActorType.MEMBERSHIP, actor_id=42)
        factory.create(org, actor_type=AuditActorType.MEMBERSHIP, actor_id=99)

        response = admin_client.get(CHANGELIST_URL + "?search=42")
        assert response.status_code == 200
        content = response.content.decode()

        # Only the record with actor_id=42 should match.
        assert "Showing 1 of 1 record" in content
        assert "<td>42</td>" in content
        assert "<td>99</td>" not in content

    def test_search_by_actor_id_non_numeric_term(self, admin_client: Client, db: Any) -> None:
        """Searching for a non-numeric term does NOT match actor_id.

        This avoids a DB type error when searching for "abc" against BigIntegerField.
        Only subject/label/type filters are checked for non-numeric terms.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(
            org,
            actor_type=AuditActorType.MEMBERSHIP,
            actor_id=42,
            subject_type="test.Model",
            subject_id="abc",
        )

        # Searching for "abc" should match the subject_id (soft ref), not try to
        # match actor_id as an integer.
        response = admin_client.get(CHANGELIST_URL + "?search=abc")
        assert response.status_code == 200
        content = response.content.decode()

        # The record matches via subject_id; no type error.
        assert "Showing 1 of 1 record" in content

    def test_search_by_actor_id_zero(self, admin_client: Client, db: Any) -> None:
        """Searching for "0" matches actor_id=0 if present.

        Zero is a valid actor_id; ensure the isdigit() check correctly handles it.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, actor_type=AuditActorType.MEMBERSHIP, actor_id=0)
        factory.create(org, actor_type=AuditActorType.MEMBERSHIP, actor_id=1)

        response = admin_client.get(CHANGELIST_URL + "?search=0")
        assert response.status_code == 200
        content = response.content.decode()

        # Only the record with actor_id=0 should match.
        assert "Showing 1 of 1 record" in content


# ---------------------------------------------------------------------------
# Filter by affected membership id
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminFilterAffectedMembership:
    """Test filtering by affected_membership_id."""

    def test_filter_by_affected_membership_id(self, admin_client: Client, db: Any) -> None:
        """Filtering by affected_membership_id shows only audits linked to that membership.

        Seed two audits; link one to membership M, the other to membership N.
        Filter by affected_membership_id=M.pk and assert only the first audit appears.
        """
        org = baker.make(Organization)
        membership_m = baker.make(OrganizationMembership, organization=org)
        membership_n = baker.make(OrganizationMembership, organization=org)

        factory = AuditFactory()
        audit_m = factory.create(org)
        audit_n = factory.create(org)

        # Link audit_m to membership_m, and audit_n to membership_n.
        AuditAffectedMembership.objects.create(
            organization_id=org.pk,
            audit_fk_id=audit_m.pk,
            membership_fk_id=membership_m.pk,
        )
        AuditAffectedMembership.objects.create(
            organization_id=org.pk,
            audit_fk_id=audit_n.pk,
            membership_fk_id=membership_n.pk,
        )

        # Filter by membership_m.pk.
        response = admin_client.get(CHANGELIST_URL + f"?affected_membership_id={membership_m.pk}")
        assert response.status_code == 200
        content = response.content.decode()

        # Only the audit linked to membership_m should appear.
        assert "Showing 1 of 1 record" in content

    def test_filter_by_affected_membership_id_multiple_links(
        self, admin_client: Client, db: Any
    ) -> None:
        """An audit can be linked to multiple memberships; filtering finds audits linked to any.

        Seed an audit linked to both membership_m and membership_n.
        Filter by affected_membership_id=membership_m.pk and verify the audit is returned.
        """
        org = baker.make(Organization)
        membership_m = baker.make(OrganizationMembership, organization=org)
        membership_n = baker.make(OrganizationMembership, organization=org)

        factory = AuditFactory()
        audit = factory.create(org)

        # Link the audit to both memberships.
        AuditAffectedMembership.objects.create(
            organization_id=org.pk,
            audit_fk_id=audit.pk,
            membership_fk_id=membership_m.pk,
        )
        AuditAffectedMembership.objects.create(
            organization_id=org.pk,
            audit_fk_id=audit.pk,
            membership_fk_id=membership_n.pk,
        )

        # Filter by membership_m; the audit should still be returned.
        response = admin_client.get(CHANGELIST_URL + f"?affected_membership_id={membership_m.pk}")
        assert response.status_code == 200
        content = response.content.decode()

        # The audit is linked to membership_m, so it appears.
        assert "Showing 1 of 1 record" in content

    def test_filter_by_affected_membership_id_no_match(self, admin_client: Client, db: Any) -> None:
        """Filtering by an unlinked membership returns zero rows without error.

        Seed two audits, neither linked to membership M.
        Filter by affected_membership_id=M.pk and assert no rows are returned.
        """
        org = baker.make(Organization)
        membership_m = baker.make(OrganizationMembership, organization=org)

        factory = AuditFactory()
        factory.create(org)
        factory.create(org)

        # Neither audit is linked to membership_m, so the filter returns zero.
        response = admin_client.get(CHANGELIST_URL + f"?affected_membership_id={membership_m.pk}")
        assert response.status_code == 200
        assert b"No audit records found." in response.content

    def test_filter_by_affected_membership_id_invalid_value(
        self, admin_client: Client, db: Any
    ) -> None:
        """Non-numeric affected_membership_id is ignored (treated as None).

        Pass affected_membership_id=abc and assert the filter is skipped
        (no crash, no filter applied).
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org)

        # Non-numeric value should be parsed to None and the filter skipped.
        response = admin_client.get(CHANGELIST_URL + "?affected_membership_id=abc")
        assert response.status_code == 200
        content = response.content.decode()

        # The record should still be returned because the filter is ignored.
        assert "Showing 1 of 1 record" in content

    def test_filter_by_affected_membership_id_empty_value(
        self, admin_client: Client, db: Any
    ) -> None:
        """Empty affected_membership_id is treated as None (no filter).

        Pass affected_membership_id= (empty) and assert all records are returned.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org)
        factory.create(org)

        # Empty value should be parsed to None and the filter skipped.
        response = admin_client.get(CHANGELIST_URL + "?affected_membership_id=")
        assert response.status_code == 200
        content = response.content.decode()

        # All records should be returned.
        assert "Showing 2 of 2 record" in content


# ---------------------------------------------------------------------------
# Integration with repository query
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminSearchRepositoryIntegration:
    """Verify that search and affected_membership_id are passed through to the repository.

    Uses the stub-repository pattern from test_admin_list.py to assert that
    _build_audit_query correctly populates AuditQuery.search and
    affected_membership_id fields.
    """

    def test_search_param_passed_to_repository(self, admin_client: Client, db: Any) -> None:
        """Verify that ?search=<term> populates AuditQuery.search.

        Uses a stub repository to capture the AuditQuery and assert search is set.
        """
        from di_core.containers import container

        stub = StubRepositoryCapture()

        assert container is not None, "DI container must be initialized"
        with container.audit_repository.override(stub):
            response = admin_client.get(CHANGELIST_URL + "?search=test-search-term")

        assert response.status_code == 200
        assert stub.last_query is not None
        assert stub.last_query.search == "test-search-term"

    def test_affected_membership_id_param_passed_to_repository(
        self, admin_client: Client, db: Any
    ) -> None:
        """Verify that ?affected_membership_id=<int> populates AuditQuery.affected_membership_id.

        Uses a stub repository to capture the AuditQuery and assert affected_membership_id is set.
        """
        from di_core.containers import container

        stub = StubRepositoryCapture()

        assert container is not None, "DI container must be initialized"
        with container.audit_repository.override(stub):
            response = admin_client.get(CHANGELIST_URL + "?affected_membership_id=42")

        assert response.status_code == 200
        assert stub.last_query is not None
        assert stub.last_query.affected_membership_id == 42

    def test_both_search_and_affected_membership_id(self, admin_client: Client, db: Any) -> None:
        """Both search and affected_membership_id can be combined in one request.

        Issues a request with both params and asserts they both reach the repository.
        """
        from di_core.containers import container

        stub = StubRepositoryCapture()

        assert container is not None, "DI container must be initialized"
        with container.audit_repository.override(stub):
            response = admin_client.get(
                CHANGELIST_URL + "?search=test-term&affected_membership_id=99"
            )

        assert response.status_code == 200
        assert stub.last_query is not None
        assert stub.last_query.search == "test-term"
        assert stub.last_query.affected_membership_id == 99


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminSearchEdgeCases:
    """Test edge cases for search and affected_membership_id filtering."""

    def test_empty_search_term(self, admin_client: Client, db: Any) -> None:
        """Empty search string is treated as None (no filter).

        Pass ?search= (empty) and assert all records are returned.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org)
        factory.create(org)

        response = admin_client.get(CHANGELIST_URL + "?search=")
        assert response.status_code == 200
        content = response.content.decode()

        # All records should be returned.
        assert "Showing 2 of 2 record" in content

    def test_search_case_insensitive(self, admin_client: Client, db: Any) -> None:
        """Search is case-insensitive (__icontains in the repository).

        Seed a record with subject_type="Calendar.Event" and search for "calendar".
        Should match due to icontains.
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, subject_type="Calendar.Event")

        response = admin_client.get(CHANGELIST_URL + "?search=calendar")
        assert response.status_code == 200
        content = response.content.decode()

        # The case-insensitive match should work.
        assert "Showing 1 of 1 record" in content

    def test_search_with_special_characters(self, admin_client: Client, db: Any) -> None:
        """Search with special characters doesn't crash.

        Test that searching for a string with special chars (e.g. "test@domain")
        is handled gracefully by icontains (no regex error).
        """
        org = baker.make(Organization)
        factory = AuditFactory()
        factory.create(org, subject_id="test@domain.com")

        response = admin_client.get(CHANGELIST_URL + "?search=test@domain")
        assert response.status_code == 200
        # Should not crash; match should work via icontains.
        assert "Showing 1 of 1 record" in response.content.decode()
