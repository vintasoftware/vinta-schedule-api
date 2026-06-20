"""Phase 3 — EventAttendance: expand + backfill tests.

Covers the schema and data-migration steps that add ``membership_user_id``
(and the ``membership`` ForeignObject descriptor) to ``EventAttendance``
alongside the kept ``user`` FK.

Null-user rows
--------------
The plan mentions that the sync path creates ``EventAttendance(user=None, ...)``
rows. In practice the DB column ``user_id`` is ``NOT NULL``, so such rows cannot
be inserted directly. The backfill SQL defensively guards ``AND ea.user_id IS NOT
NULL`` for forward compatibility, but there are no live null-user rows to test
with. This is a pre-existing schema/code inconsistency that Phase 3 does not
change.

Scenarios tested
----------------
- **Matched row** (user set, membership exists): backfill sets
  ``membership_user_id == user_id`` and the ``membership`` descriptor resolves
  to the correct ``OrganizationMembership``.
- **Orphan row** (``user_id IS NOT NULL`` but no matching membership): stays
  ``membership_user_id = NULL`` and appears in ``collect_orphans()``.
- **Idempotency**: running the backfill twice does not change already-populated
  rows.
- **Behaviour-unchanged**: existing ``user``-based reads (``filter(user=...)``,
  ``{a.user_id: a}`` dict map, ``event.attendances.filter(user=...)``
  queryset) return identical results after the backfill.
- **Multi-org isolation**: backfill populates rows across multiple organisations
  independently; an orphan in one org does not affect a matched row in another.
- **CSV report path**: monkeypatched ``LOG_DIR`` + verified file + column order.
- **OSError fallback**: write failure falls back to per-row WARNING logs.
- **Reverse**: ``reverse_backfill_membership_user_id`` clears all
  ``membership_user_id`` values.
"""

from __future__ import annotations

import csv
import importlib
import logging
import uuid
from unittest.mock import patch

import pytest
from model_bakery import baker

from calendar_integration.migrations._0029_backfill_helpers import (
    backfill_membership_user_id_sql,
    collect_orphans,
)
from calendar_integration.models import CalendarEvent, EventAttendance
from organizations.models import Organization, OrganizationMembership


# Lazy import of the migration module (starts with a digit; use importlib).
_migration_module = importlib.import_module(
    "calendar_integration.migrations.0029_backfill_eventattendance_membership_user_id"
)
backfill_membership_user_id = _migration_module.backfill_membership_user_id
reverse_backfill_membership_user_id = _migration_module.reverse_backfill_membership_user_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(organization: Organization) -> CalendarEvent:
    """Create a minimal CalendarEvent in the given org.

    ``external_id`` is set to a UUID string to avoid the unique constraint
    on ``CalendarEvent.external_id`` when multiple events are created across
    test cases that share a database.
    """
    calendar = baker.make("calendar_integration.Calendar", organization=organization)
    return baker.make(
        "calendar_integration.CalendarEvent",
        calendar_fk=calendar,
        organization=organization,
        external_id=str(uuid.uuid4()),
        start_time_tz_unaware="2026-01-01 10:00:00",
        end_time_tz_unaware="2026-01-01 11:00:00",
        timezone="UTC",
    )


def _make_attendance_with_user(organization: Organization, user, event: CalendarEvent):
    """Create an EventAttendance with user_id set (standard path)."""
    return EventAttendance.objects.create(
        organization=organization,
        event=event,
        user=user,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def member_user(organization):
    user = baker.make("users.User")
    OrganizationMembership.objects.create(user=user, organization=organization)
    return user


@pytest.fixture
def event(organization) -> CalendarEvent:
    return _make_event(organization)


# ---------------------------------------------------------------------------
# Schema assertions (field present and nullable after migration)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_membership_user_id_column_is_nullable(organization, member_user, event):
    """New rows can be created with membership_user_id=NULL (field is nullable)."""
    attendance = EventAttendance.objects.create(
        organization=organization,
        event=event,
        user=member_user,
        membership_user_id=None,
    )
    attendance.refresh_from_db()
    assert attendance.membership_user_id is None


# ---------------------------------------------------------------------------
# Matched rows — backfill sets membership_user_id
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_backfill_sets_membership_user_id_for_matched_rows(organization, member_user, event):
    """Rows with a matching OrganizationMembership get membership_user_id = user_id."""
    attendance = _make_attendance_with_user(organization, member_user, event)
    assert attendance.membership_user_id is None  # pre-backfill

    backfill_membership_user_id_sql()

    attendance.refresh_from_db()
    assert attendance.membership_user_id == member_user.id


@pytest.mark.django_db
def test_membership_descriptor_resolves_after_backfill(organization, member_user, event):
    """The ``membership`` ForeignObject descriptor resolves after backfill."""
    attendance = _make_attendance_with_user(organization, member_user, event)
    backfill_membership_user_id_sql()

    resolved = (
        EventAttendance.objects.filter_by_organization(organization.id)
        .select_related("membership")
        .get(pk=attendance.pk)
    )
    assert resolved.membership is not None
    assert resolved.membership.user_id == member_user.id
    assert resolved.membership.organization_id == organization.id


# ---------------------------------------------------------------------------
# Orphan rows — user set but no membership
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_orphan_stays_null_membership_user_id(organization, event):
    """Rows with user_id set but no membership remain membership_user_id=NULL."""
    non_member = baker.make("users.User")  # NOT a member of organization
    attendance = _make_attendance_with_user(organization, non_member, event)

    backfill_membership_user_id_sql()

    attendance.refresh_from_db()
    assert attendance.membership_user_id is None


@pytest.mark.django_db
def test_orphan_appears_in_collect_orphans(organization, event):
    """collect_orphans() returns the (attendance_id, user_id, org_id, event_id) tuple."""
    non_member = baker.make("users.User")
    attendance = _make_attendance_with_user(organization, non_member, event)

    backfill_membership_user_id_sql()
    orphans = collect_orphans()

    assert len(orphans) == 1
    orphan_id, orphan_user_id, orphan_org_id, _orphan_event_id = orphans[0]
    assert orphan_id == attendance.id
    assert orphan_user_id == non_member.id
    assert orphan_org_id == organization.id


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_backfill_is_idempotent(organization, member_user, event):
    """Running the backfill twice does not change already-populated rows."""
    _make_attendance_with_user(organization, member_user, event)

    backfill_membership_user_id_sql()
    backfill_membership_user_id_sql()  # second call — must be a no-op

    attendance = EventAttendance.objects.filter_by_organization(organization.id).get(
        user_id=member_user.id
    )
    assert attendance.membership_user_id == member_user.id


# ---------------------------------------------------------------------------
# Behaviour-unchanged — existing user-based reads are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_user_filter_still_works_after_backfill(organization, member_user, event):
    """``filter(user=...)`` queryset still works after the backfill."""
    attendance = _make_attendance_with_user(organization, member_user, event)
    backfill_membership_user_id_sql()

    qs = EventAttendance.objects.filter_by_organization(organization.id).filter(user=member_user)
    assert qs.count() == 1
    assert qs.first().pk == attendance.pk


@pytest.mark.django_db
def test_user_id_dict_map_still_works_after_backfill(organization, event):
    """``{a.user_id: a}`` dict construction (service pattern) is unaffected."""
    user1 = baker.make("users.User")
    user2 = baker.make("users.User")
    OrganizationMembership.objects.create(user=user1, organization=organization)
    OrganizationMembership.objects.create(user=user2, organization=organization)
    a1 = _make_attendance_with_user(organization, user1, event)
    a2 = _make_attendance_with_user(organization, user2, event)

    backfill_membership_user_id_sql()

    attendances = list(
        EventAttendance.objects.filter_by_organization(organization.id).filter(event=event)
    )
    attendance_map = {a.user_id: a for a in attendances}

    assert attendance_map[user1.id].pk == a1.pk
    assert attendance_map[user2.id].pk == a2.pk


@pytest.mark.django_db
def test_event_attendances_queryset_filter_still_works(organization, member_user, event):
    """``event.attendances.filter(user=...)`` queryset path is unaffected."""
    attendance = _make_attendance_with_user(organization, member_user, event)
    backfill_membership_user_id_sql()

    qs = event.attendances.filter(user=member_user)
    assert qs.count() == 1
    assert qs.first().pk == attendance.pk


# ---------------------------------------------------------------------------
# Multi-org isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_multi_org_backfill(organization):
    """Backfill operates correctly across multiple organisations."""
    org2 = baker.make(Organization)
    user1 = baker.make("users.User")
    user2 = baker.make("users.User")
    OrganizationMembership.objects.create(user=user1, organization=organization)
    OrganizationMembership.objects.create(user=user2, organization=org2)

    event1 = _make_event(organization)
    event2 = _make_event(org2)
    a1 = _make_attendance_with_user(organization, user1, event1)
    a2 = _make_attendance_with_user(org2, user2, event2)

    backfill_membership_user_id_sql()

    a1.refresh_from_db()
    a2.refresh_from_db()
    assert a1.membership_user_id == user1.id
    assert a2.membership_user_id == user2.id


@pytest.mark.django_db
def test_multi_org_orphan_in_one_org_does_not_affect_other(organization):
    """An orphan row in one org does not block backfill in another."""
    org2 = baker.make(Organization)
    member = baker.make("users.User")
    non_member = baker.make("users.User")  # has no membership in org2
    OrganizationMembership.objects.create(user=member, organization=organization)

    event1 = _make_event(organization)
    event2 = _make_event(org2)
    matched = _make_attendance_with_user(organization, member, event1)
    orphan = _make_attendance_with_user(org2, non_member, event2)

    backfill_membership_user_id_sql()

    matched.refresh_from_db()
    orphan.refresh_from_db()
    assert matched.membership_user_id == member.id
    assert orphan.membership_user_id is None


# ---------------------------------------------------------------------------
# CSV report path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_csv_report_written_for_orphans(tmp_path, organization, event):
    """The backfill writes a CSV report for orphan rows."""
    non_member = baker.make("users.User")
    attendance = _make_attendance_with_user(organization, non_member, event)

    with patch.object(_migration_module, "LOG_DIR", str(tmp_path)):
        backfill_membership_user_id(apps=None, schema_editor=None)

    csv_files = list(tmp_path.glob("eventattendance_orphans_*.csv"))
    assert len(csv_files) == 1

    with open(csv_files[0]) as f:
        rows = list(csv.reader(f))

    assert rows[0] == ["attendance_id", "user_id", "organization_id", "event_id"]
    assert len(rows) == 2  # header + 1 orphan
    assert int(rows[1][0]) == attendance.id
    assert int(rows[1][1]) == non_member.id
    assert int(rows[1][2]) == organization.id


@pytest.mark.django_db
def test_no_csv_written_when_no_orphans(tmp_path, organization, member_user, event):
    """When there are no orphans, no CSV file is written."""
    _make_attendance_with_user(organization, member_user, event)

    with patch.object(_migration_module, "LOG_DIR", str(tmp_path)):
        backfill_membership_user_id(apps=None, schema_editor=None)

    csv_files = list(tmp_path.glob("eventattendance_orphans_*.csv"))
    assert len(csv_files) == 0


# ---------------------------------------------------------------------------
# OSError fallback — per-row log when CSV cannot be written
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_oserror_fallback_logs_orphans(caplog, organization, event):
    """When the CSV write fails, orphan rows are logged individually."""
    non_member = baker.make("users.User")
    attendance = _make_attendance_with_user(organization, non_member, event)

    with (
        patch.object(_migration_module, "LOG_DIR", "/nonexistent/readonly/path"),
        caplog.at_level(logging.WARNING),
        patch("builtins.open", side_effect=OSError("read-only")),
    ):
        backfill_membership_user_id(apps=None, schema_editor=None)

    logged = "\n".join(caplog.messages)
    assert str(attendance.id) in logged
    assert str(non_member.id) in logged


# ---------------------------------------------------------------------------
# Reverse migration
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reverse_clears_membership_user_id(organization, member_user, event):
    """reverse_backfill_membership_user_id sets membership_user_id = NULL for all rows."""
    attendance = _make_attendance_with_user(organization, member_user, event)
    backfill_membership_user_id_sql()
    attendance.refresh_from_db()
    assert attendance.membership_user_id == member_user.id  # pre-reverse

    reverse_backfill_membership_user_id(apps=None, schema_editor=None)

    attendance.refresh_from_db()
    assert attendance.membership_user_id is None
