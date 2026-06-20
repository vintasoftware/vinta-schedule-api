"""Phase 5 integration tests — CalendarManagementToken membership expand + backfill.

Mirrors Phase 3 (test_attendance_expand.py, now deleted) for CalendarManagementToken.

Covers:
- Matched rows (user + org membership) get membership_user_id == user_id after
  backfill and the .membership ForeignObject descriptor resolves correctly.
- Null-user (external-attendee) tokens stay NULL in membership_user_id and are
  NOT reported as orphans.
- Orphan rows (user set but no OrganizationMembership) stay NULL and appear in
  the orphan report.
- Backfill is idempotent (running twice does not change any rows).
- Existing user-based reads (filter(user=...), token.user_id) are unchanged.
- CSV report is written when LOG_DIR is writable (monkeypatched path); when the
  path is unwritable, orphans are logged individually (OSError fallback).
- Reverse: membership_user_id is cleared to NULL for all rows.
- Multi-org: tokens from different organizations are each evaluated against their
  own organization's memberships.
"""

from __future__ import annotations

import csv
import importlib
import logging

import pytest
from model_bakery import baker

from calendar_integration.migrations._0034_backfill_helpers import (
    backfill_membership_user_id_sql,
    collect_orphans,
)
from calendar_integration.models import CalendarManagementToken
from common.utils.authentication_utils import generate_long_lived_token, hash_long_lived_token
from organizations.models import Organization, OrganizationMembership


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(org: Organization, user=None, external_attendee=None) -> CalendarManagementToken:
    """Create a minimal CalendarManagementToken for backfill tests."""
    token_str = generate_long_lived_token()
    hashed_token = hash_long_lived_token(token_str)
    return CalendarManagementToken.objects.create(
        organization=org,
        token_hash=hashed_token,
        user=user,
        external_attendee=external_attendee,
    )


def _migration_mod():
    """Import the data migration module by dotted path."""
    return importlib.import_module(
        "calendar_integration.migrations.0034_backfill_calendarmanagementtoken_membership_user_id"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def other_org(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def member_user(org):
    """A user who is a member of org."""
    user = baker.make("users.User")
    OrganizationMembership.objects.create(user=user, organization=org)
    return user


@pytest.fixture
def orphan_user():
    """A user who is NOT a member of any organization."""
    return baker.make("users.User")


# ---------------------------------------------------------------------------
# Backfill: matched (member) token
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_backfill_sets_membership_user_id_for_member_token(org, member_user):
    """Tokens with a membership-matched (user_id, org_id) get membership_user_id = user_id."""
    token = _make_token(org, user=member_user)
    assert token.membership_user_id is None  # pre-backfill

    backfill_membership_user_id_sql()

    token.refresh_from_db()
    assert token.membership_user_id == member_user.id


@pytest.mark.django_db
def test_membership_descriptor_resolves_after_backfill(org, member_user):
    """After backfill, the .membership ForeignObject descriptor resolves to OrganizationMembership."""
    token = _make_token(org, user=member_user)
    backfill_membership_user_id_sql()
    token.refresh_from_db()

    membership = OrganizationMembership.objects.get(user=member_user, organization=org)
    resolved = token.membership
    assert resolved is not None
    assert resolved.user_id == membership.user_id
    assert resolved.organization_id == membership.organization_id


# ---------------------------------------------------------------------------
# Backfill: null-user (external-attendee) token — NOT an orphan
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_null_user_token_stays_null_and_is_not_reported(org):
    """Tokens with user=NULL (external-attendee backed) stay null after backfill and are not orphans."""
    token = _make_token(org, user=None)
    assert token.user_id is None

    backfill_membership_user_id_sql()
    token.refresh_from_db()

    assert token.membership_user_id is None

    orphans = collect_orphans()
    orphan_ids = [row[0] for row in orphans]
    assert token.id not in orphan_ids


# ---------------------------------------------------------------------------
# Backfill: orphan token (user set but no membership)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_orphan_token_stays_null_and_is_reported(org, orphan_user):
    """Tokens with a user but no OrganizationMembership stay null and appear in collect_orphans()."""
    token = _make_token(org, user=orphan_user)

    backfill_membership_user_id_sql()
    token.refresh_from_db()

    assert token.membership_user_id is None

    orphans = collect_orphans()
    orphan_ids = [row[0] for row in orphans]
    assert token.id in orphan_ids

    # The orphan tuple is (token_id, user_id, organization_id)
    orphan_row = next(row for row in orphans if row[0] == token.id)
    assert orphan_row[1] == orphan_user.id
    assert orphan_row[2] == org.id


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_backfill_is_idempotent(org, member_user):
    """Running the backfill twice does not change already-populated rows."""
    token = _make_token(org, user=member_user)

    backfill_membership_user_id_sql()
    token.refresh_from_db()
    first_value = token.membership_user_id

    backfill_membership_user_id_sql()
    token.refresh_from_db()
    assert token.membership_user_id == first_value


# ---------------------------------------------------------------------------
# Existing user-based reads are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_existing_user_filter_unchanged(org, member_user):
    """filter(user=...) and .user_id still work after backfill (behaviour-preserving)."""
    token = _make_token(org, user=member_user)

    backfill_membership_user_id_sql()
    token.refresh_from_db()

    assert token.user_id == member_user.id
    found = CalendarManagementToken.objects.filter_by_organization(org.id).filter(user=member_user)
    assert found.filter(id=token.id).exists()


# ---------------------------------------------------------------------------
# CSV report
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_csv_report_written_for_orphans(org, orphan_user, tmp_path, monkeypatch):
    """Orphan rows cause a CSV file to be written under LOG_DIR."""
    mod = _migration_mod()
    monkeypatch.setattr(mod, "LOG_DIR", str(tmp_path))

    token = _make_token(org, user=orphan_user)
    backfill_membership_user_id_sql()

    mod.backfill_membership_user_id(None, None)

    csv_files = list(tmp_path.glob("calendarmanagementtoken_orphans_*.csv"))
    assert len(csv_files) == 1

    with open(csv_files[0], newline="") as f:
        rows = list(csv.reader(f))

    assert rows[0] == ["token_id", "user_id", "organization_id"]
    data_rows = rows[1:]
    assert any(int(r[0]) == token.id for r in data_rows)


@pytest.mark.django_db
def test_oserror_fallback_logs_individually(org, orphan_user, tmp_path, monkeypatch, caplog):
    """When CSV write fails (OSError), orphans are logged individually."""
    mod = _migration_mod()

    # Point LOG_DIR at a path that will fail to mkdir (use a file where a dir is expected).
    bad_path = tmp_path / "not_a_dir"
    bad_path.write_text("block")
    monkeypatch.setattr(mod, "LOG_DIR", str(bad_path / "subdir"))

    _make_token(org, user=orphan_user)
    backfill_membership_user_id_sql()

    with caplog.at_level(logging.WARNING):
        mod.backfill_membership_user_id(None, None)

    # At least two warnings: the count/path message + one per-orphan row
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) >= 2


# ---------------------------------------------------------------------------
# Reverse path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reverse_clears_membership_user_id(org, member_user):
    """The reverse function sets membership_user_id = NULL on all rows."""
    mod = _migration_mod()

    token = _make_token(org, user=member_user)
    backfill_membership_user_id_sql()
    token.refresh_from_db()
    assert token.membership_user_id is not None

    mod.reverse_backfill_membership_user_id(None, None)
    token.refresh_from_db()
    assert token.membership_user_id is None


# ---------------------------------------------------------------------------
# Multi-org
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_multi_org_backfill_scoped_to_own_membership(org, other_org):
    """Tokens are each matched against their own organization's memberships."""
    user_a = baker.make("users.User")
    user_b = baker.make("users.User")

    # user_a is a member of org only; user_b is a member of other_org only.
    OrganizationMembership.objects.create(user=user_a, organization=org)
    OrganizationMembership.objects.create(user=user_b, organization=other_org)

    token_a = _make_token(org, user=user_a)  # matched
    token_b = _make_token(other_org, user=user_b)  # matched
    # user_a token in other_org → orphan (user_a has no membership in other_org)
    token_a_cross = _make_token(other_org, user=user_a)

    backfill_membership_user_id_sql()

    token_a.refresh_from_db()
    token_b.refresh_from_db()
    token_a_cross.refresh_from_db()

    assert token_a.membership_user_id == user_a.id
    assert token_b.membership_user_id == user_b.id
    assert token_a_cross.membership_user_id is None  # orphan: user_a not in other_org

    orphans = collect_orphans()
    orphan_ids = [row[0] for row in orphans]
    assert token_a_cross.id in orphan_ids
    assert token_a.id not in orphan_ids
    assert token_b.id not in orphan_ids
