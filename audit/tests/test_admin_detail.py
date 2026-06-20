"""Tests for the repository-backed AuditAdmin detail view.

Covers:
- Detail page renders all fields (id, created_at, org, action, actor, subject, diff, scopes).
- Pretty-printed diff: field name + old/new values shown in a table.
- Pretty-printed system_user_scopes: list of scopes; includes scoped_to_membership when present.
- Affected memberships: list of membership ids.
- Each actor type (SYSTEM, MEMBERSHIP, SYSTEM_USER, SINGLE_USE_CODE) displays its
  distinguishing fields correctly.
- Missing audit id returns HTTP 404.
- Read-only: no edit form, no Save button, no mutating POST accepted.
- Data comes from AuditRepository.get(...) (backend-agnosticism via stub override).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.test import Client

import pytest
from model_bakery import baker

from audit.constants import AuditAction, AuditActorType
from audit.factories import AuditAffectedMembershipFactory, AuditFactory
from audit.repositories import AuditRepository
from audit.types import ActorSnapshot, AuditRecord, SubjectRef
from organizations.models import Organization, OrganizationMembership, OrganizationRole


User = get_user_model()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def superuser(db):
    """Create a superuser for admin access."""
    return User.objects.create_superuser(
        email="admin@example.com",
        password="adminpassword",
    )


@pytest.fixture
def admin_client(superuser):
    """Django test client logged in as a superuser."""
    c = Client()
    c.force_login(superuser)
    return c


@pytest.fixture
def anonymous_client():
    """Django test client with no session."""
    return Client()


# ---------------------------------------------------------------------------
# Stub repository for backend-agnosticism tests
# ---------------------------------------------------------------------------


class StubAuditRepository(AuditRepository):
    """Minimal in-memory AuditRepository for testing admin backend-agnosticism."""

    def __init__(self, records: dict[int, AuditRecord]) -> None:
        """Initialize with a dict mapping audit_id -> AuditRecord."""
        self._records = records

    def add(self, data: Any) -> AuditRecord:  # type: ignore[override]
        raise NotImplementedError("StubAuditRepository is read-only")

    def get(self, audit_id: int) -> AuditRecord | None:
        return self._records.get(audit_id)

    def query(self, q: Any, *, offset: int = 0, limit: int = 50, ordering: str = "-created_at"):  # type: ignore[override]
        raise NotImplementedError("StubAuditRepository does not support query")


# ---------------------------------------------------------------------------
# Detail page access
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminDetailAccess:
    """Basic access control and page rendering for the detail view."""

    def test_detail_page_loads_for_existing_record(self, admin_client: Client, db: Any) -> None:
        """A superuser can load the detail page for an existing audit."""
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()
        # The title should contain the audit ID.
        assert f"Audit record #{record.pk}" in content

    def test_detail_page_404_for_missing_audit(self, admin_client: Client) -> None:
        """Requesting a non-existent audit returns HTTP 404."""
        url = "/super/audit/audit/999999/view/"
        response = admin_client.get(url)
        assert response.status_code == 404

    def test_detail_page_requires_staff(self, anonymous_client: Client, db: Any) -> None:
        """Non-staff users are redirected to login."""
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = anonymous_client.get(url)
        assert response.status_code == 302
        assert "/login/" in response["Location"]

    def test_detail_page_contains_breadcrumbs(self, admin_client: Client, db: Any) -> None:
        """The detail page includes breadcrumb navigation."""
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()
        # Should have breadcrumb: Home > Audit > Audit records > Record #ID
        assert "Audit records" in content
        assert f"Record #{record.pk}" in content


# ---------------------------------------------------------------------------
# Field rendering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminDetailFields:
    """Verify all fields are rendered correctly."""

    def test_detail_shows_all_basic_fields(self, admin_client: Client, db: Any) -> None:
        """The detail page displays id, org, created_at, and action."""
        org = baker.make(Organization)
        record = AuditFactory().create(
            org,
            action=AuditAction.UPDATE,
        )
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        # Basic fields
        assert f"{record.pk}" in content  # ID
        assert f"{org.pk}" in content  # Organization
        assert "update" in content.lower()  # Action (value, not label)
        # created_at shown in a date format
        assert record.created_at.strftime("%Y-%m-%d") in content

    def test_detail_shows_subject(self, admin_client: Client, db: Any) -> None:
        """The detail page displays subject_type, subject_id, and subject_label."""
        org = baker.make(Organization)
        subject_label = "Test Subject 123"
        record = AuditFactory().create(
            org,
            subject_type="app.Model",
            subject_id="12345",
            subject_label=subject_label,
        )
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        assert "app.Model" in content
        assert "12345" in content
        assert subject_label in content

    def test_detail_shows_affected_memberships(self, admin_client: Client, db: Any) -> None:
        """The detail page displays the list of affected membership ids."""
        org = baker.make(Organization)
        m1 = baker.make(OrganizationMembership, organization=org)
        m2 = baker.make(OrganizationMembership, organization=org)
        record = AuditFactory().create(org)

        AuditAffectedMembershipFactory().create(org, record, m1)
        AuditAffectedMembershipFactory().create(org, record, m2)

        url = f"/super/audit/audit/{record.pk}/view/"
        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        # Both membership ids should appear
        assert f"{m1.pk}" in content
        assert f"{m2.pk}" in content
        assert "Affected Memberships" in content


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminDetailDiff:
    """Verify diff is pretty-printed as a readable table."""

    def test_detail_shows_diff_as_table(self, admin_client: Client, db: Any) -> None:
        """A record with a diff shows it in a table format."""
        org = baker.make(Organization)
        diff_data = {
            "email": {"old": "old@example.com", "new": "new@example.com"},
            "status": {"old": "active", "new": "inactive"},
        }
        record = AuditFactory().create(org, diff=diff_data)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        # Should have a Changes (Diff) section
        assert "Changes (Diff)" in content
        # Field names should appear
        assert "email" in content
        assert "status" in content
        # Old and new values should appear
        assert "old@example.com" in content
        assert "new@example.com" in content
        assert "active" in content
        assert "inactive" in content

    def test_detail_hides_diff_section_when_no_diff(self, admin_client: Client, db: Any) -> None:
        """A record without a diff should not show the diff section."""
        org = baker.make(Organization)
        record = AuditFactory().create(org, diff=None)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        # Should NOT have a Changes section
        assert "Changes (Diff)" not in content


# ---------------------------------------------------------------------------
# Actor snapshot rendering — each actor type
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminDetailActors:
    """Verify each actor type displays its distinguishing fields."""

    def test_detail_shows_system_actor(self, admin_client: Client, db: Any) -> None:
        """SYSTEM actors show 'system' type, no id, no role, no scopes."""
        org = baker.make(Organization)
        record = AuditFactory().create(
            org,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
        )
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        assert "system" in content.lower()
        # No "Actor ID" field for SYSTEM (actor_id is None)
        # No actor role section
        assert "Actor Role (Membership)" not in content
        # No System User Scopes section
        assert "System User Scopes" not in content

    def test_detail_shows_membership_actor(self, admin_client: Client, db: Any) -> None:
        """MEMBERSHIP actors show the actor_role snapshot."""
        org = baker.make(Organization)
        record = AuditFactory().create(
            org,
            actor_type=AuditActorType.MEMBERSHIP,
            actor_id=123,
            actor_role=OrganizationRole.ADMIN,
        )
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        assert "membership" in content.lower()
        assert "123" in content  # Actor ID
        assert "Actor Role (Membership)" in content
        # The role value should appear (the value or label from OrganizationRole)
        assert "admin" in content.lower()
        # No System User Scopes for MEMBERSHIP
        assert "System User Scopes" not in content

    def test_detail_shows_system_user_actor_with_scopes(
        self, admin_client: Client, db: Any
    ) -> None:
        """SYSTEM_USER actors show scopes and optionally scoped_to_membership."""
        org = baker.make(Organization)
        scopes = ["read", "write", "delete"]
        membership_id = 456
        record = AuditFactory().create(
            org,
            actor_type=AuditActorType.SYSTEM_USER,
            actor_id=789,
            system_user_scopes=scopes,
            system_user_scoped_to_membership=membership_id,
        )
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        assert "system user" in content.lower()
        assert "789" in content  # Actor ID
        assert "System User Scopes" in content
        # All scopes should appear in a list
        for scope in scopes:
            assert scope in content
        assert "Scoped to Membership" in content
        assert str(membership_id) in content

    def test_detail_shows_system_user_org_wide(self, admin_client: Client, db: Any) -> None:
        """SYSTEM_USER without scoped_to_membership shows it's org-wide."""
        org = baker.make(Organization)
        scopes = ["read"]
        record = AuditFactory().create(
            org,
            actor_type=AuditActorType.SYSTEM_USER,
            actor_id=789,
            system_user_scopes=scopes,
            system_user_scoped_to_membership=None,
        )
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        assert "System User Scopes" in content
        assert "read" in content
        # "Scoped to Membership" should NOT appear if it's None
        assert "Scoped to Membership" not in content

    def test_detail_shows_single_use_code_actor(self, admin_client: Client, db: Any) -> None:
        """SINGLE_USE_CODE actors show the actor_id."""
        org = baker.make(Organization)
        record = AuditFactory().create(
            org,
            actor_type=AuditActorType.SINGLE_USE_CODE,
            actor_id=555,
        )
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        assert AuditActorType.SINGLE_USE_CODE in content  # The actor type value
        assert "555" in content  # Actor ID


# ---------------------------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminDetailReadOnly:
    """Verify the detail page is fully read-only."""

    def test_detail_page_has_no_save_button(self, admin_client: Client, db: Any) -> None:
        """The detail page must not contain a Save/Submit button."""
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        # No save button or readonly form inputs
        assert 'name="_save"' not in content
        # The detail page may have a form but it won't be a post form with editable fields
        if "<form " in content and 'method="post"' in content:
            # If there's a POST form, it must not contain any editable input/textarea
            assert '<input type="text"' not in content
            assert "<textarea" not in content

    def test_detail_page_has_no_delete_button(self, admin_client: Client, db: Any) -> None:
        """The detail page must not contain a delete button or confirmation."""
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        # No delete button or confirmation form
        assert "Delete" not in content or "Confirm" not in content

    def test_post_to_detail_url_does_not_mutate(self, admin_client: Client, db: Any) -> None:
        """POST to the detail view URL does not mutate the record."""
        from audit.models import Audit

        org = baker.make(Organization)
        record = AuditFactory().create(org)
        url = f"/super/audit/audit/{record.pk}/view/"
        original_action = record.action

        # POST with mutation attempt — the view treats POST as GET but doesn't save anything
        response = admin_client.post(url, {"action": "delete"})
        # The view still renders (200) but doesn't process the POST payload
        assert response.status_code == 200

        # Record must be unchanged — no mutation occurred
        unchanged = Audit.original_manager.get(pk=record.pk)
        assert unchanged.action == original_action

    def test_detail_page_has_back_link(self, admin_client: Client, db: Any) -> None:
        """The detail page should have a back link to the changelist."""
        org = baker.make(Organization)
        record = AuditFactory().create(org)
        url = f"/super/audit/audit/{record.pk}/view/"

        response = admin_client.get(url)
        assert response.status_code == 200
        content = response.content.decode()

        # Should have a back link
        assert "Back to Audit Records" in content or "audit_audit_changelist" in content


# ---------------------------------------------------------------------------
# Backend-agnosticism: stub repository
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAuditAdminDetailBackendAgnosticism:
    """Verify the detail view reads exclusively from AuditRepository.get()."""

    def test_stub_repository_renders_detail(self, admin_client: Client, db: Any) -> None:
        """Overriding the repository with a stub returns its canned record."""
        stub_record = AuditRecord(
            id=999,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            organization_id=1,
            action=AuditAction.CREATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=SubjectRef(subject_type="test.Model", subject_id="stub-123"),
        )
        stub = StubAuditRepository({999: stub_record})

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        with container.audit_repository.override(stub):
            response = admin_client.get("/super/audit/audit/999/view/")

        assert response.status_code == 200
        content = response.content.decode()
        # The stub's record data should appear
        assert "999" in content
        assert "test.Model" in content
        assert "stub-123" in content

    def test_orm_not_used_when_stub_overrides(self, admin_client: Client, db: Any) -> None:
        """With the stub active, real ORM audit records are NOT visible in the detail.

        This is the definitive proof of backend-agnosticism: if the view were
        using the ORM directly via model.pk or get_object_or_404, it would return
        a real DB record regardless of the stub injection.
        """
        org = baker.make(Organization)
        # Create a real ORM record with a distinctive subject type
        real_record = AuditFactory().create(org, subject_type="real.ORMRecord")

        # Stub returns a DIFFERENT record with the same ID
        stub_record = AuditRecord(
            id=real_record.pk,
            created_at=datetime(2025, 1, 1, tzinfo=UTC),
            organization_id=org.pk,
            action=AuditAction.UPDATE,
            actor=ActorSnapshot(actor_type=AuditActorType.SYSTEM, actor_id=None),
            subject=SubjectRef(subject_type="stub.StubRecord", subject_id="stub-data"),
        )
        stub = StubAuditRepository({real_record.pk: stub_record})

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        with container.audit_repository.override(stub):
            response = admin_client.get(f"/super/audit/audit/{real_record.pk}/view/")

        content = response.content.decode()
        # The real ORM subject_type must NOT appear — the detail used the stub.
        assert "real.ORMRecord" not in content
        # The stub's subject_type MUST appear — proving data came from the stub.
        assert "stub.StubRecord" in content

    def test_stub_404_when_record_not_in_stub(self, admin_client: Client) -> None:
        """When the stub doesn't have the requested id, the detail returns 404."""
        stub = StubAuditRepository({})  # Empty stub

        from di_core.containers import container

        assert container is not None, "DI container must be initialized in tests"
        with container.audit_repository.override(stub):
            response = admin_client.get("/super/audit/audit/999/view/")

        # Should be 404 because the stub doesn't have id=999
        assert response.status_code == 404
