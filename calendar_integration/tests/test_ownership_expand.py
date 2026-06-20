"""Integration tests for Phase 1 — CalendarOwnership membership expand + backfill.

Covers:
- CalendarOwnership rows whose (user_id, organization_id) has a matching
  OrganizationMembership are backfilled: membership_user_id == user_id, and
  the .membership descriptor resolves to the correct OrganizationMembership.
- Orphan rows (no OrganizationMembership for that (user_id, organization_id))
  are left with membership_user_id IS NULL and appear in the orphan report
  returned from the backfill function.
- Behaviour-unchanged assertions: existing reads via .user / filter(user=...) /
  the reverse accessor user.calendar_ownerships all return identical results.
- The backfill is idempotent: running it twice yields the same outcome.
"""

from __future__ import annotations

import importlib

import pytest
from model_bakery import baker

from calendar_integration.migrations._0023_backfill_helpers import (
    backfill_membership_user_id_sql,
    collect_orphans,
)
from calendar_integration.models import Calendar, CalendarOwnership
from organizations.models import Organization, OrganizationMembership


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_org() -> Organization:
    return baker.make("organizations.Organization")


def _create_user():
    return baker.make("users.User")


def _create_membership(user, org) -> OrganizationMembership:
    return OrganizationMembership.objects.create(
        user=user,
        organization=org,
    )


def _create_calendar(org: Organization) -> Calendar:
    return baker.make(Calendar, organization=org)


def _create_ownership(user, calendar: Calendar) -> CalendarOwnership:
    return CalendarOwnership.objects.create(
        user=user,
        calendar_fk=calendar,
        organization=calendar.organization,
    )


def _run_backfill() -> list[tuple]:
    """Run the backfill SQL and return the orphan list."""
    backfill_membership_user_id_sql()
    return collect_orphans()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def org(db) -> Organization:
    return _create_org()


@pytest.fixture
def user_with_membership(org):
    """A user who is a member of org."""
    user = _create_user()
    _create_membership(user, org)
    return user


@pytest.fixture
def user_without_membership(org):
    """A user who has NO membership in org (orphan scenario)."""
    return _create_user()


@pytest.fixture
def calendar(org) -> Calendar:
    return _create_calendar(org)


@pytest.fixture
def matched_ownership(user_with_membership, calendar) -> CalendarOwnership:
    """Ownership row that should be backfilled (membership exists)."""
    return _create_ownership(user_with_membership, calendar)


@pytest.fixture
def orphan_ownership(user_without_membership, calendar) -> CalendarOwnership:
    """Ownership row that should remain NULL (no membership for this user+org)."""
    return _create_ownership(user_without_membership, calendar)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_backfill_sets_membership_user_id_for_matched_rows(matched_ownership, user_with_membership):
    """Rows with a matching OrganizationMembership get membership_user_id == user_id."""
    assert matched_ownership.membership_user_id is None  # pre-backfill state

    _run_backfill()

    matched_ownership.refresh_from_db()
    assert matched_ownership.membership_user_id == user_with_membership.pk


@pytest.mark.django_db(transaction=True)
def test_backfill_membership_descriptor_resolves(matched_ownership, user_with_membership, org):
    """After backfill, .membership resolves to the correct OrganizationMembership."""
    _run_backfill()

    matched_ownership.refresh_from_db()
    membership = OrganizationMembership.objects.get(user=user_with_membership, organization=org)
    # Re-fetch through the ORM to trigger the ForeignObject join.
    ownership_with_rel = (
        CalendarOwnership.original_manager.filter(pk=matched_ownership.pk)
        .select_related("membership")
        .first()
    )
    assert ownership_with_rel is not None
    assert ownership_with_rel.membership == membership
    assert ownership_with_rel.membership.user_id == user_with_membership.pk
    assert ownership_with_rel.membership.organization_id == org.pk


@pytest.mark.django_db(transaction=True)
def test_orphan_stays_null_after_backfill(orphan_ownership):
    """Orphan rows (no matching membership) keep membership_user_id = NULL."""
    _run_backfill()

    orphan_ownership.refresh_from_db()
    assert orphan_ownership.membership_user_id is None


@pytest.mark.django_db(transaction=True)
def test_orphan_appears_in_report(orphan_ownership, user_without_membership, org, calendar):
    """The orphan collect function returns the orphan row's identity columns."""
    # Run backfill first (which skips the orphan).
    backfill_membership_user_id_sql()
    orphans = collect_orphans()

    # The orphan row must appear in the report.
    orphan_ids = {row[0] for row in orphans}
    assert orphan_ownership.pk in orphan_ids

    # Verify the columns: (ownership_id, user_id, organization_id, calendar_id)
    orphan_row = next(r for r in orphans if r[0] == orphan_ownership.pk)
    assert orphan_row[1] == user_without_membership.pk
    assert orphan_row[2] == org.pk
    assert orphan_row[3] == calendar.pk


@pytest.mark.django_db(transaction=True)
def test_matched_ownership_not_in_orphan_report(matched_ownership):
    """Matched rows do NOT appear in the orphan report after backfill."""
    backfill_membership_user_id_sql()
    orphans = collect_orphans()

    orphan_ids = {row[0] for row in orphans}
    assert matched_ownership.pk not in orphan_ids


@pytest.mark.django_db(transaction=True)
def test_backfill_is_idempotent(matched_ownership, orphan_ownership, user_with_membership):
    """Running the backfill twice produces the same result (no double-write side-effects)."""
    _run_backfill()
    _run_backfill()

    matched_ownership.refresh_from_db()
    orphan_ownership.refresh_from_db()

    assert matched_ownership.membership_user_id == user_with_membership.pk
    assert orphan_ownership.membership_user_id is None


@pytest.mark.django_db(transaction=True)
def test_behaviour_unchanged_filter_by_user(matched_ownership, user_with_membership, org):
    """Filtering by user= still works as before (behaviour unchanged)."""
    _run_backfill()

    qs = CalendarOwnership.original_manager.filter(
        user=user_with_membership,
        organization=org,
    )
    assert qs.count() == 1
    assert qs.first().pk == matched_ownership.pk


@pytest.mark.django_db(transaction=True)
def test_behaviour_unchanged_reverse_accessor(matched_ownership, user_with_membership, org):
    """user.calendar_ownerships reverse accessor still works as before.

    CalendarOwnership extends OrganizationModel, so its manager requires an
    organization filter.  The test mirrors how production code uses this accessor:
    always scoped by organization.
    """
    _run_backfill()

    ownerships = list(user_with_membership.calendar_ownerships.filter(organization=org))
    assert len(ownerships) == 1
    assert ownerships[0].pk == matched_ownership.pk


@pytest.mark.django_db(transaction=True)
def test_multiple_orgs_and_users(db):
    """Backfill handles multiple orgs/users correctly; only matched rows get populated."""
    org1 = _create_org()
    org2 = _create_org()

    user_a = _create_user()  # member of org1, not org2
    user_b = _create_user()  # member of org2, not org1
    user_c = _create_user()  # member of neither (orphan in both)

    _create_membership(user_a, org1)
    _create_membership(user_b, org2)

    cal1 = _create_calendar(org1)
    cal2 = _create_calendar(org2)

    own_a1 = _create_ownership(user_a, cal1)  # matched
    own_b2 = _create_ownership(user_b, cal2)  # matched
    own_c1 = _create_ownership(user_c, cal1)  # orphan
    own_c2 = _create_ownership(user_c, cal2)  # orphan

    _run_backfill()

    own_a1.refresh_from_db()
    own_b2.refresh_from_db()
    own_c1.refresh_from_db()
    own_c2.refresh_from_db()

    assert own_a1.membership_user_id == user_a.pk
    assert own_b2.membership_user_id == user_b.pk
    assert own_c1.membership_user_id is None
    assert own_c2.membership_user_id is None


@pytest.mark.django_db(transaction=True)
def test_backfill_migration_writes_csv_report(
    orphan_ownership,
    matched_ownership,
    user_without_membership,
    org,
    calendar,
    tmp_path,
    monkeypatch,
):
    """backfill_membership_user_id() writes a CSV containing orphan rows only."""
    migration_module = importlib.import_module(
        "calendar_integration.migrations.0023_backfill_calendarownership_membership_user_id"
    )
    monkeypatch.setattr(migration_module, "LOG_DIR", str(tmp_path))

    migration_module.backfill_membership_user_id(None, None)

    csv_files = list(tmp_path.glob("calendarownership_orphans_*.csv"))
    assert len(csv_files) == 1, "Expected exactly one CSV report file"
    csv_path = csv_files[0]

    import csv as csv_mod

    with open(csv_path, newline="") as f:
        reader = csv_mod.reader(f)
        rows = list(reader)

    assert rows[0] == ["ownership_id", "user_id", "organization_id", "calendar_id"]
    data_rows = rows[1:]
    data_ids = {int(r[0]) for r in data_rows}
    assert orphan_ownership.pk in data_ids, "Orphan row must appear in CSV"
    assert matched_ownership.pk not in data_ids, "Matched row must NOT appear in CSV"


@pytest.mark.django_db(transaction=True)
def test_backfill_migration_oserror_fallback(orphan_ownership, caplog, monkeypatch):
    """When CSV write fails with OSError, each orphan row is logged individually."""
    import logging

    migration_module = importlib.import_module(
        "calendar_integration.migrations.0023_backfill_calendarownership_membership_user_id"
    )

    def raising_makedirs(path, **kwargs):
        raise OSError("simulated read-only filesystem")

    monkeypatch.setattr(migration_module.os, "makedirs", raising_makedirs)

    with caplog.at_level(logging.WARNING, logger=migration_module.logger.name):
        migration_module.backfill_membership_user_id(None, None)

    warning_texts = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(orphan_ownership.pk) in t for t in warning_texts), (
        "Expected per-orphan warning log line containing the orphan ownership pk"
    )


@pytest.mark.django_db(transaction=True)
def test_reverse_backfill_clears_membership_user_id(matched_ownership, user_with_membership):
    """reverse_backfill_membership_user_id sets membership_user_id = NULL on all rows."""
    migration_module = importlib.import_module(
        "calendar_integration.migrations.0023_backfill_calendarownership_membership_user_id"
    )

    # First populate the field.
    backfill_membership_user_id_sql()
    matched_ownership.refresh_from_db()
    assert matched_ownership.membership_user_id == user_with_membership.pk

    # Now reverse.
    migration_module.reverse_backfill_membership_user_id(None, None)

    matched_ownership.refresh_from_db()
    assert matched_ownership.membership_user_id is None
