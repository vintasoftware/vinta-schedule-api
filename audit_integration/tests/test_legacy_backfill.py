"""The migration that moves the old ``audit`` app's rows into the new log.

This runs once, against real data, and cannot be re-run to fix a bad outcome
without understanding what it already did -- so its properties are worth pinning
down rather than discovering in production.

The legacy tables are built here by hand. They belong to an app that has been
deleted, so there is no model and no migration state to lean on; the migration
reads them with raw SQL and these tests write them the same way.
"""

import uuid
from datetime import UTC, datetime
from importlib import import_module

from django.db import connection

import pytest
from vinta_audit_logs.models import Audit, AuditAction, AuditAffectedIdentity

from audit_integration.models import OrganizationAuditIdentity, OrganizationAuditScope
from organizations.models import Organization


pytestmark = pytest.mark.django_db


def backfill_module():
    """The migration module, imported on demand.

    By name because a module whose name starts with a digit cannot be reached
    with ``from ... import``, and lazily because the DI container wires this app
    by importing every module in it -- a module-level import here would make the
    whole project fail to start whenever this migration is renamed or squashed.
    """
    return import_module("audit_integration.migrations.0002_backfill_from_legacy_audit")


LEGACY_AUDIT_DDL = """
CREATE TABLE audit_audit (
    id BIGSERIAL PRIMARY KEY,
    uid UUID NOT NULL UNIQUE,
    organization_id BIGINT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    action VARCHAR(100) NOT NULL,
    actor_type VARCHAR(20) NOT NULL,
    actor_id BIGINT NULL,
    actor_role VARCHAR(20) NULL,
    system_user_scopes JSONB NULL,
    system_user_scoped_to_membership BIGINT NULL,
    subject_type VARCHAR(255) NOT NULL,
    subject_id VARCHAR(255) NOT NULL,
    subject_label VARCHAR(255) NULL,
    diff JSONB NULL
)
"""

LEGACY_AFFECTED_DDL = """
CREATE TABLE audit_auditaffectedmembership (
    id BIGSERIAL PRIMARY KEY,
    organization_id BIGINT NULL,
    audit_fk_id BIGINT NOT NULL,
    membership_user_id BIGINT NOT NULL
)
"""


#: The legacy table as it stands in a database that never ran
#: ``audit/migrations/0003`` -- the migration that added ``uid`` and that shipped
#: in the same pull request which deleted the ``audit`` app, so an environment
#: without a deploy in between never applied it. This is the shape production
#: actually has, and the backfill has to read it.
LEGACY_AUDIT_DDL_WITHOUT_UID = LEGACY_AUDIT_DDL.replace("    uid UUID NOT NULL UNIQUE,\n", "")


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Legacy Org")


@pytest.fixture
def legacy_tables():
    """Stand the deleted app's tables back up for the duration of one test."""
    with connection.cursor() as cursor:
        cursor.execute(LEGACY_AUDIT_DDL)
        cursor.execute(LEGACY_AFFECTED_DDL)
    yield
    # The surrounding test transaction rolls these back, but dropping them
    # explicitly keeps a transactional test from leaking them into the next one.
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS audit_auditaffectedmembership")
        cursor.execute("DROP TABLE IF EXISTS audit_audit")


@pytest.fixture
def legacy_tables_without_uid():
    """The same tables, minus the column ``audit/migrations/0003`` never added."""
    with connection.cursor() as cursor:
        cursor.execute(LEGACY_AUDIT_DDL_WITHOUT_UID)
        cursor.execute(LEGACY_AFFECTED_DDL)
    yield
    with connection.cursor() as cursor:
        cursor.execute("DROP TABLE IF EXISTS audit_auditaffectedmembership")
        cursor.execute("DROP TABLE IF EXISTS audit_audit")


def _insert_legacy_without_uid(organization_id, **overrides) -> int:
    """Write one row of the uid-less legacy table, returning its primary key."""
    row = {
        "organization_id": organization_id,
        "created_at": datetime(2026, 2, 2, 10, 0, tzinfo=UTC),
        "action": "create",
        "actor_type": "membership",
        "actor_id": 7,
        "subject_type": "organizations.OrganizationMembership",
        "subject_id": "7",
    }
    row.update(overrides)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO audit_audit ({", ".join(row)})
            VALUES ({", ".join(["%s"] * len(row))}) RETURNING id
            """,  # noqa: S608 - identifiers are this module's own literals
            list(row.values()),
        )
        return cursor.fetchone()[0]


def _insert_legacy(organization_id, **overrides) -> uuid.UUID:
    """Write one legacy audit row, returning its uid."""
    row = {
        "uid": uuid.uuid7(),
        "organization_id": organization_id,
        "created_at": datetime(2026, 2, 2, 10, 0, tzinfo=UTC),
        "action": "create",
        "actor_type": "membership",
        "actor_id": 7,
        "actor_role": "admin",
        "system_user_scopes": None,
        "system_user_scoped_to_membership": None,
        "subject_type": "organizations.OrganizationMembership",
        "subject_id": "7",
        "subject_label": "someone",
        "diff": None,
    }
    row.update(overrides)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO audit_audit (
                uid, organization_id, created_at, action, actor_type, actor_id,
                actor_role, system_user_scopes, system_user_scoped_to_membership,
                subject_type, subject_id, subject_label, diff
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)
            """,
            [
                row["uid"],
                row["organization_id"],
                row["created_at"],
                row["action"],
                row["actor_type"],
                row["actor_id"],
                row["actor_role"],
                row["system_user_scopes"],
                row["system_user_scoped_to_membership"],
                row["subject_type"],
                row["subject_id"],
                row["subject_label"],
                row["diff"],
            ],
        )
    return row["uid"]


def _run_backfill():
    """Run the migration's function against the live models.

    ``django.apps.apps`` stands in for the historical registry: the schema here is
    the current one, which is what the migration would see running as the last
    migration in the chain.
    """
    from django.apps import apps
    from django.db import connection as conn

    class _SchemaEditorStub:
        connection = conn

    backfill_module().backfill(apps, _SchemaEditorStub())


def test_no_legacy_table_is_a_no_op():
    """A database that never had the old app -- a fresh install -- is left alone."""
    _run_backfill()

    assert Audit.objects.count() == 0


def test_records_move_with_their_identity_intact(legacy_tables, organization):
    """The uid survives, because it is the record's identity in every repository."""
    uid = _insert_legacy(organization.id)

    _run_backfill()

    moved = Audit.objects.get(uid=uid)
    assert moved.action_key == "create"
    assert moved.created_at == datetime(2026, 2, 2, 10, 0, tzinfo=UTC)
    assert moved.scope_key == str(organization.id)
    assert moved.actor_type == "membership"
    assert moved.actor_key == "7"


def test_subject_types_are_normalized_to_the_new_spelling(legacy_tables, organization):
    """Old rows said ``ModelName``; new ones say ``modelname``.

    Left alone, a filter by subject type would silently return only half the
    history -- everything before the move, or everything after, but never both.
    """
    _insert_legacy(organization.id, subject_type="organizations.OrganizationMembership")

    _run_backfill()

    assert Audit.objects.get().subject_content_type_key == "organizations.organizationmembership"


def test_actor_context_survives_as_columns_and_metadata(legacy_tables, organization):
    """The role and token scopes land in both places, as they do at runtime."""
    _insert_legacy(
        organization.id,
        actor_type="system_user",
        actor_id=42,
        actor_role=None,
        system_user_scopes='["calendars"]',
        system_user_scoped_to_membership=9,
    )

    _run_backfill()

    identity = OrganizationAuditIdentity.objects.get()
    assert identity.system_user_scopes == ["calendars"]
    assert identity.system_user_scoped_to_membership == 9
    assert identity.metadata["system_user_scopes"] == ["calendars"]
    # A system-user id is an API token's, not a person's: claiming otherwise
    # would attribute the action to whichever user happens to hold that pk.
    assert identity.user_id is None


def test_a_membership_actor_keeps_its_user_link(legacy_tables, organization):
    """Only a membership actor is a real user, and that link is worth keeping."""
    from model_bakery import baker

    from users.models import User

    user = baker.make(User)
    _insert_legacy(organization.id, actor_type="membership", actor_id=user.pk)

    _run_backfill()

    assert OrganizationAuditIdentity.objects.get().user_id == user.pk


def test_an_actor_whose_account_is_gone_still_moves(legacy_tables, organization):
    """A deleted account must not take the migration down with it.

    ``user`` is a real foreign key now where the legacy table held a bare
    integer, so this is the case that would abort the whole backfill. The record
    still says who acted -- that is what ``identity_key`` is for.
    """
    _insert_legacy(organization.id, actor_type="membership", actor_id=999_999)

    _run_backfill()

    identity = OrganizationAuditIdentity.objects.get()
    assert identity.user_id is None
    assert identity.identity_key == "999999"


def test_affected_memberships_become_linked_identities(legacy_tables, organization):
    """The old through table's rows become identity snapshots plus links."""
    uid = _insert_legacy(organization.id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT id FROM audit_audit WHERE uid = %s", [uid])
        legacy_id = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO audit_auditaffectedmembership "
            "(organization_id, audit_fk_id, membership_user_id) VALUES (%s, %s, %s), (%s, %s, %s)",
            [organization.id, legacy_id, 11, organization.id, legacy_id, 12],
        )

    _run_backfill()

    links = AuditAffectedIdentity.objects.filter(audit__uid=uid)
    assert sorted(link.identity_key for link in links) == ["11", "12"]
    assert all(link.identity_type == "membership" for link in links)


def test_running_twice_moves_each_record_once(legacy_tables, organization):
    """Re-running after a partial failure resumes; it does not duplicate.

    The uid already on every legacy row is what makes this possible -- a row whose
    uid is present in the new table is simply skipped.
    """
    for _ in range(3):
        _insert_legacy(organization.id)

    _run_backfill()
    _run_backfill()

    assert Audit.objects.count() == 3
    assert OrganizationAuditIdentity.objects.count() == 3


def test_dimensions_are_deduplicated_across_records(legacy_tables, organization):
    """Many records, one scope row and one action row."""
    for _ in range(4):
        _insert_legacy(organization.id)

    _run_backfill()

    assert OrganizationAuditScope.objects.filter(scope_key=str(organization.id)).count() == 1
    assert AuditAction.objects.filter(key="create").count() == 1


def test_a_global_legacy_row_lands_in_the_global_scope(legacy_tables):
    """A row with no organization becomes a global record, not a broken one."""
    _insert_legacy(None)

    _run_backfill()

    moved = Audit.objects.get()
    assert moved.scope_type == "global"
    assert moved.scope_key == ""
    assert OrganizationAuditScope.objects.get(scope_key="").organization_id is None


def test_the_walk_crosses_batch_boundaries(legacy_tables, organization, monkeypatch):
    """Every row moves, not just the first batch."""
    monkeypatch.setattr(backfill_module(), "BATCH_SIZE", 2)
    for _ in range(5):
        _insert_legacy(organization.id)

    _run_backfill()

    assert Audit.objects.count() == 5


def test_a_legacy_table_without_uid_still_moves(legacy_tables_without_uid, organization):
    """The shape production is actually in: no ``uid`` column to read.

    ``audit/migrations/0003`` added that column in the same pull request that
    deleted the ``audit`` app, so it never ran anywhere that had not deployed in
    between. Selecting ``uid`` there fails outright, which is what took the
    deploy down.
    """
    legacy_id = _insert_legacy_without_uid(organization.id)

    _run_backfill()

    moved = Audit.objects.get()
    assert moved.uid == backfill_module()._derived_uid(legacy_id)
    assert moved.action_key == "create"
    assert moved.scope_key == str(organization.id)
    assert moved.actor_key == "7"


def test_a_legacy_table_without_uid_runs_twice_safely(legacy_tables_without_uid, organization):
    """Idempotency survives the missing column, because the uid is derived from
    the legacy primary key rather than generated per run."""
    for _ in range(3):
        _insert_legacy_without_uid(organization.id)

    _run_backfill()
    _run_backfill()

    assert Audit.objects.count() == 3
    assert OrganizationAuditIdentity.objects.count() == 3


def test_affected_memberships_move_without_a_uid_column(legacy_tables_without_uid, organization):
    """The affected-party links hang off the legacy primary key, which is there
    whether or not the uid column is."""
    legacy_id = _insert_legacy_without_uid(organization.id)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO audit_auditaffectedmembership "
            "(organization_id, audit_fk_id, membership_user_id) VALUES (%s, %s, %s)",
            [organization.id, legacy_id, 11],
        )

    _run_backfill()

    links = AuditAffectedIdentity.objects.all()
    assert [link.identity_key for link in links] == ["11"]
