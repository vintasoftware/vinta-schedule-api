"""Tests for CSV export functionality in the audit admin."""

import csv
import json
from datetime import UTC, datetime
from io import StringIO

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

import pytest

from audit.constants import AuditAction, AuditActorType
from audit.models import Audit, AuditAffectedMembership
from organizations.models import Organization, OrganizationMembership, OrganizationRole


User = get_user_model()


class TestAuditAdminExportAccess:
    """Test access control and basic export endpoint behavior."""

    @pytest.fixture
    def admin_client(self, db):
        """Logged-in admin client."""
        admin_user = User.objects.create_superuser(email="admin@test.com", password="test")
        client = Client()
        client.force_login(admin_user)
        return client

    @pytest.fixture
    def org(self, db):
        """Test organization."""
        return Organization.objects.create(name="Test Org")

    def test_anonymous_redirects_to_login(self, client, db):
        """GET /admin/audit/audit/export/ without auth redirects to login."""
        response = client.get(reverse("admin:audit_audit_export"))
        assert response.status_code == 302
        assert "/login" in response.url

    def test_non_staff_user_forbidden(self, db):
        """Non-staff user is redirected to login (not authenticated for admin)."""
        user = User.objects.create_user(email="user@test.com", password="test")
        # Non-staff users don't have access to admin, so they're redirected
        client = Client()
        client.force_login(user)
        response = client.get(reverse("admin:audit_audit_export"))
        # Non-staff is redirected to login (not allowed)
        assert response.status_code == 302
        assert "/login" in response.url

    def test_superuser_can_access(self, admin_client):
        """Superuser can access the export endpoint."""
        response = admin_client.get(reverse("admin:audit_audit_export"))
        assert response.status_code == 200
        assert "text/csv" in response["Content-Type"]
        assert "attachment" in response["Content-Disposition"]

    def test_staff_user_can_access(self, admin_client, db):
        """Staff (non-superuser) can access the export endpoint."""
        staff_user = User.objects.create_user(email="staff@test.com", password="test")
        staff_user.is_staff = True
        staff_user.save()
        client = Client()
        client.force_login(staff_user)
        response = client.get(reverse("admin:audit_audit_export"))
        assert response.status_code == 200

    def test_content_disposition_header(self, admin_client):
        """Response has correct Content-Disposition header for download."""
        response = admin_client.get(reverse("admin:audit_audit_export"))
        assert response["Content-Disposition"] == "attachment; filename=audit_export.csv"

    def test_content_type_is_csv(self, admin_client):
        """Response has Content-Type: text/csv; charset=utf-8."""
        response = admin_client.get(reverse("admin:audit_audit_export"))
        assert response.status_code == 200
        assert response["Content-Type"] == "text/csv; charset=utf-8"

    def test_export_is_read_only_get_only(self, admin_client):
        """POST to export endpoint returns 405 Method Not Allowed."""
        response = admin_client.post(reverse("admin:audit_audit_export"))
        assert response.status_code == 405


class TestAuditAdminExportStructure:
    """Test CSV header and structure."""

    @pytest.fixture
    def admin_client(self, db):
        """Logged-in admin client."""
        admin_user = User.objects.create_superuser(email="admin@test.com", password="test")
        client = Client()
        client.force_login(admin_user)
        return client

    @pytest.fixture
    def org(self, db):
        """Test organization."""
        return Organization.objects.create(name="Test Org")

    def test_empty_export_has_header_only(self, admin_client, org):
        """Export with no audit records contains only the CSV header."""
        response = admin_client.get(
            reverse("admin:audit_audit_export"), {"organization_id": str(org.id)}
        )
        assert response.status_code == 200
        content = b"".join(response.streaming_content).decode("utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 1  # Header only
        reader = csv.DictReader(StringIO(content))
        assert reader.fieldnames == [
            "id",
            "created_at",
            "organization_id",
            "action",
            "actor_type",
            "actor_id",
            "actor_role",
            "system_user_scopes",
            "system_user_scoped_to_membership",
            "subject_type",
            "subject_id",
            "subject_label",
            "affected_membership_ids",
            "diff",
        ]

    def test_export_with_one_record(self, admin_client, org):
        """Export with one audit record includes header + one data row."""
        # Create a minimal audit record
        audit = Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="123",
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 2  # Header + 1 data row
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["id"] == str(audit.id)
        assert rows[0]["action"] == AuditAction.CREATE
        assert rows[0]["actor_type"] == AuditActorType.SYSTEM
        assert rows[0]["actor_id"] == ""
        assert rows[0]["subject_type"] == "app.Model"
        assert rows[0]["subject_id"] == "123"

    def test_created_at_isoformat(self, admin_client, org):
        """created_at is exported in ISO format."""
        audit = Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="1",
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        # Verify the created_at is in ISO format
        assert rows[0]["created_at"] == audit.created_at.isoformat()
        # Also verify it starts with a date pattern
        assert rows[0]["created_at"].startswith("202")


class TestAuditAdminExportFilters:
    """Test that export respects active filters."""

    @pytest.fixture
    def admin_client(self, db):
        """Logged-in admin client."""
        admin_user = User.objects.create_superuser(email="admin@test.com", password="test")
        client = Client()
        client.force_login(admin_user)
        return client

    @pytest.fixture
    def org(self, db):
        """Test organization."""
        return Organization.objects.create(name="Test Org")

    @pytest.fixture
    def audit_records(self, org):
        """Create multiple audit records for filtering tests.

        Records are backdated via update() after creation (auto_now_add prevents
        setting created_at on create) to well-separated timestamps so that
        date-range filter tests can use unambiguous boundaries.

        Timestamps:
        - Record 1 (CREATE):  2020-01-01T00:00:00Z
        - Record 2 (UPDATE):  2022-06-01T00:00:00Z
        - Record 3 (DELETE):  2024-01-01T00:00:00Z
        """
        records = []

        # Record 1: action=CREATE
        r1 = Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="1",
        )
        Audit.original_manager.filter(pk=r1.pk).update(created_at=datetime(2020, 1, 1, tzinfo=UTC))
        r1.refresh_from_db()
        records.append(r1)

        # Record 2: action=UPDATE with diff
        r2 = Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.UPDATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="2",
            diff={"field": {"old": "old_value", "new": "new_value"}},
        )
        Audit.original_manager.filter(pk=r2.pk).update(created_at=datetime(2022, 6, 1, tzinfo=UTC))
        r2.refresh_from_db()
        records.append(r2)

        # Record 3: action=DELETE, different actor type
        r3 = Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.DELETE,
            actor_type=AuditActorType.MEMBERSHIP,
            actor_id=999,
            actor_role=OrganizationRole.ADMIN,
            subject_type="app.Model",
            subject_id="3",
        )
        Audit.original_manager.filter(pk=r3.pk).update(created_at=datetime(2024, 1, 1, tzinfo=UTC))
        r3.refresh_from_db()
        records.append(r3)

        return records

    def test_action_filter_narrows_export(self, admin_client, org, audit_records):
        """Export with action filter includes only matching records."""
        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {"action": AuditAction.CREATE, "organization_id": str(org.id)},
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["action"] == AuditAction.CREATE
        assert rows[0]["subject_id"] == "1"

    def test_actor_type_filter_narrows_export(self, admin_client, org, audit_records):
        """Export with actor_type filter includes only matching records."""
        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {"actor_type": AuditActorType.MEMBERSHIP, "organization_id": str(org.id)},
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["actor_type"] == AuditActorType.MEMBERSHIP
        assert rows[0]["actor_id"] == "999"
        assert rows[0]["action"] == AuditAction.DELETE

    def test_has_diff_filter_narrows_export(self, admin_client, org, audit_records):
        """Export with has_diff=yes includes only records with diff."""
        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {"has_diff": "yes", "organization_id": str(org.id)},
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        # Record 2 is the UPDATE with diff
        assert rows[0]["action"] == AuditAction.UPDATE
        assert rows[0]["subject_id"] == "2"

    def test_has_diff_no_filter_narrows_export(self, admin_client, org, audit_records):
        """Export with has_diff=no includes only records without diff."""
        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {"has_diff": "no", "organization_id": str(org.id)},
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 2
        # Records 1 and 3 have no diff
        subject_ids = {row["subject_id"] for row in rows}
        assert subject_ids == {"1", "3"}

    def test_search_filter_narrows_export(self, admin_client, org, audit_records):
        """Export with search filter includes only matching records."""
        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {"search": "2", "organization_id": str(org.id)},
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["subject_id"] == "2"

    def test_created_after_filter_narrows_export(self, admin_client, org, audit_records):
        """Export with created_after filter excludes older records.

        Records are at 2020-01-01, 2022-06-01, 2024-01-01 UTC.
        Filtering with created_after = 2021-01-01Z should return only
        records 2 and 3 (those strictly after the boundary).
        """
        # Boundary between record 1 (2020) and record 2 (2022).
        after_time = "2021-01-01T00:00:00Z"
        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {
                "created_after": after_time,
                "organization_id": str(org.id),
            },
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        # Only records 2 and 3 are after 2021-01-01.
        assert len(rows) == 2
        returned_ids = {int(r["id"]) for r in rows}
        assert returned_ids == {audit_records[1].id, audit_records[2].id}

    def test_created_before_filter_narrows_export(self, admin_client, org, audit_records):
        """Export with created_before filter excludes newer records.

        Records are at 2020-01-01, 2022-06-01, 2024-01-01 UTC.
        Filtering with created_before = 2023-01-01Z should return only
        records 1 and 2 (those strictly before the boundary).
        """
        # Boundary between record 2 (2022-06-01) and record 3 (2024-01-01).
        before_time = "2023-01-01T00:00:00Z"
        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {
                "created_before": before_time,
                "organization_id": str(org.id),
            },
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        # Only records 1 and 2 are before 2023-01-01.
        assert len(rows) == 2
        returned_ids = {int(r["id"]) for r in rows}
        assert returned_ids == {audit_records[0].id, audit_records[1].id}

    def test_organization_filter_narrows_export(self, admin_client):
        """Export with organization_id filter includes only that org's records."""
        org1 = Organization.objects.create(name="Org 1")
        org2 = Organization.objects.create(name="Org 2")

        Audit.objects.create(
            organization_id=org1.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="1",
        )
        Audit.objects.create(
            organization_id=org2.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="2",
        )

        response = admin_client.get(
            reverse("admin:audit_audit_export"),
            {"organization_id": str(org1.id)},
        )
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["organization_id"] == str(org1.id)
        assert rows[0]["subject_id"] == "1"


class TestAuditAdminExportSerialization:
    """Test serialization of complex fields in CSV."""

    @pytest.fixture
    def admin_client(self, db):
        """Logged-in admin client."""
        admin_user = User.objects.create_superuser(email="admin@test.com", password="test")
        client = Client()
        client.force_login(admin_user)
        return client

    @pytest.fixture
    def org(self, db):
        """Test organization."""
        return Organization.objects.create(name="Test Org")

    @pytest.fixture
    def membership(self, org):
        """Test membership."""
        user = User.objects.create_user(email="member@test.com", password="test")
        return OrganizationMembership.objects.create(
            organization=org,
            user=user,
            role=OrganizationRole.ADMIN,
        )

    def test_diff_serializes_as_json_string(self, admin_client, org):
        """diff field serializes as JSON string in CSV."""
        diff_dict = {"field1": {"old": "a", "new": "b"}, "field2": {"old": 1, "new": 2}}
        Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.UPDATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="1",
            diff=diff_dict,
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        # Parse the JSON string
        diff_parsed = json.loads(rows[0]["diff"])
        assert diff_parsed == diff_dict

    def test_diff_none_maps_to_empty_string(self, admin_client, org):
        """diff=None maps to empty string in CSV."""
        Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="1",
            diff=None,
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert rows[0]["diff"] == ""

    def test_system_user_scopes_serializes_as_json(self, admin_client, org):
        """system_user_scopes serializes as JSON string."""
        scopes = ["read_calendar", "write_calendar"]
        Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM_USER,
            actor_id=42,
            system_user_scopes=scopes,
            system_user_scoped_to_membership=None,
            subject_type="app.Model",
            subject_id="1",
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        scopes_parsed = json.loads(rows[0]["system_user_scopes"])
        assert scopes_parsed == scopes

    def test_system_user_scopes_none_maps_to_empty_string(self, admin_client, org):
        """system_user_scopes=None maps to empty string."""
        Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.MEMBERSHIP,
            actor_id=1,
            actor_role=OrganizationRole.ADMIN,
            system_user_scopes=None,
            subject_type="app.Model",
            subject_id="1",
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert rows[0]["system_user_scopes"] == ""

    def test_affected_membership_ids_serializes_as_json(self, admin_client, org, membership):
        """affected_membership_ids serializes as JSON array."""
        audit = Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.UPDATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="1",
        )
        # Add affected memberships — membership_user_id is the org-scoped user_id
        AuditAffectedMembership.objects.create(
            organization_id=org.id,
            audit_fk=audit,
            membership_user_id=membership.user_id,
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == 1
        ids_parsed = json.loads(rows[0]["affected_membership_ids"])
        # affected_membership_ids are org-scoped user_ids (not membership PKs)
        assert ids_parsed == [membership.user_id]

    def test_affected_membership_ids_empty_maps_to_empty_string(self, admin_client, org):
        """empty affected_membership_ids maps to empty string."""
        Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            subject_type="app.Model",
            subject_id="1",
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert rows[0]["affected_membership_ids"] == ""

    def test_actor_role_serialized_correctly(self, admin_client, org):
        """actor_role is exported as a plain string."""
        Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.UPDATE,
            actor_type=AuditActorType.MEMBERSHIP,
            actor_id=123,
            actor_role=OrganizationRole.ADMIN,
            subject_type="app.Model",
            subject_id="1",
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert rows[0]["actor_role"] == OrganizationRole.ADMIN

    def test_actor_role_none_maps_to_empty_string(self, admin_client, org):
        """actor_role=None maps to empty string."""
        Audit.objects.create(
            organization_id=org.id,
            action=AuditAction.CREATE,
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            actor_role=None,
            subject_type="app.Model",
            subject_id="1",
        )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert rows[0]["actor_role"] == ""


class TestAuditAdminExportLargeResultSet:
    """Test streaming behavior for large result sets (no truncation)."""

    @pytest.fixture
    def admin_client(self, db):
        """Logged-in admin client."""
        admin_user = User.objects.create_superuser(email="admin@test.com", password="test")
        client = Client()
        client.force_login(admin_user)
        return client

    @pytest.fixture
    def org(self, db):
        """Test organization."""
        return Organization.objects.create(name="Test Org")

    def test_large_result_set_no_truncation(self, admin_client, org):
        """Export with >1000 records (exceeds one chunk) includes all rows."""
        num_records = 1500  # Exceeds the default chunk_size of 1000
        batch_size = 100
        for i in range(0, num_records, batch_size):
            Audit.objects.bulk_create(
                [
                    Audit(
                        organization_id=org.id,
                        action=AuditAction.CREATE,
                        actor_type=AuditActorType.SYSTEM,
                        actor_id=None,
                        subject_type="app.Model",
                        subject_id=f"{j}",
                    )
                    for j in range(i, min(i + batch_size, num_records))
                ]
            )

        response = admin_client.get(reverse("admin:audit_audit_export"))
        content = b"".join(response.streaming_content).decode("utf-8")
        lines = content.strip().split("\n")
        # Should be header + num_records data rows
        assert len(lines) == num_records + 1
        reader = csv.DictReader(StringIO(content))
        rows = list(reader)
        assert len(rows) == num_records
        # Prove no chunk-boundary duplication (not just count coincidence).
        assert len({r["id"] for r in rows}) == num_records
