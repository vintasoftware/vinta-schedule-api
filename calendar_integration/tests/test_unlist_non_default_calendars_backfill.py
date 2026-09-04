"""The 0050 backfill: unlist imported calendars that are nobody's default.

Drives ``calendar_integration.migrations._0050_backfill_helpers`` against a real
database rather than replaying the migration: the helpers are plain SQL over the
current ``calendar_integration_calendar`` / ``...calendarownership`` shape, and
0050 changes no schema, so the live tables are the same ones the migration runs
against.

Both directions are exercised, including the case the meta snapshot exists for --
a calendar that was ACTIVE with sync already switched off must not come back from
a reverse as ACTIVE *and* syncing.
"""

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType, CalendarVisibility
from calendar_integration.migrations._0050_backfill_helpers import (
    BACKFILL_META_KEY,
    restore_unlisted_calendars,
    unlist_non_default_calendars,
)
from calendar_integration.models import Calendar, CalendarOwnership
from organizations.models import Organization, OrganizationMembership
from users.models import User


pytestmark = pytest.mark.django_db


@pytest.fixture
def organization():
    return baker.make(Organization)


def _make_calendar(
    organization,
    *,
    external_id,
    calendar_type=CalendarType.PERSONAL,
    provider=CalendarProvider.GOOGLE,
    visibility=CalendarVisibility.ACTIVE,
    sync_enabled=True,
):
    return Calendar.objects.create(
        organization=organization,
        name=external_id,
        external_id=external_id,
        provider=provider,
        calendar_type=calendar_type,
        visibility=visibility,
        sync_enabled=sync_enabled,
    )


def _make_default_ownership(organization, calendar):
    """Mark ``calendar`` as some member's default calendar."""
    user = baker.make(User)
    OrganizationMembership.objects.get_or_create(user=user, organization=organization)
    return CalendarOwnership.objects.create(
        organization=organization,
        calendar=calendar,
        membership_user_id=user.id,
        is_default=True,
    )


def _reload(calendar):
    return Calendar.objects.filter_by_organization(calendar.organization_id).get(pk=calendar.pk)


def test_unlists_non_default_active_personal_calendar(organization):
    """An active, syncing calendar nobody marked default is unlisted and stops syncing."""
    calendar = _make_calendar(organization, external_id="holidays")

    assert unlist_non_default_calendars() == 1

    calendar = _reload(calendar)
    assert calendar.visibility == CalendarVisibility.UNLISTED
    assert calendar.sync_enabled is False
    assert calendar.meta[BACKFILL_META_KEY] == {
        "visibility": CalendarVisibility.ACTIVE.value,
        "sync_enabled": True,
    }


def test_leaves_the_default_calendar_alone(organization):
    """A calendar some member owns as their default keeps syncing."""
    calendar = _make_calendar(organization, external_id="primary")
    _make_default_ownership(organization, calendar)

    assert unlist_non_default_calendars() == 0

    calendar = _reload(calendar)
    assert calendar.visibility == CalendarVisibility.ACTIVE
    assert calendar.sync_enabled is True
    assert BACKFILL_META_KEY not in calendar.meta


@pytest.mark.parametrize(
    ("calendar_type", "provider"),
    [
        (CalendarType.RESOURCE, CalendarProvider.GOOGLE),
        (CalendarType.VIRTUAL, CalendarProvider.INTERNAL),
        (CalendarType.BUNDLE, CalendarProvider.INTERNAL),
        (CalendarType.PERSONAL, CalendarProvider.INTERNAL),
    ],
)
def test_leaves_non_imported_calendar_types_alone(organization, calendar_type, provider):
    """Rooms, bundles, and app-created calendars were never part of the account import."""
    calendar = _make_calendar(
        organization,
        external_id=f"{calendar_type}-{provider}",
        calendar_type=calendar_type,
        provider=provider,
    )

    assert unlist_non_default_calendars() == 0

    calendar = _reload(calendar)
    assert calendar.visibility == CalendarVisibility.ACTIVE
    assert calendar.sync_enabled is True


@pytest.mark.parametrize(
    "visibility",
    [CalendarVisibility.UNLISTED, CalendarVisibility.INACTIVE],
)
def test_leaves_non_active_calendars_alone(organization, visibility):
    """Unlisted-but-syncing is a deliberate choice, and inactive rows are soft-deleted."""
    calendar = _make_calendar(organization, external_id="chosen", visibility=visibility)

    assert unlist_non_default_calendars() == 0

    calendar = _reload(calendar)
    assert calendar.visibility == visibility
    assert calendar.sync_enabled is True


def test_forward_is_idempotent(organization):
    """A second forward pass changes nothing and keeps the original snapshot."""
    calendar = _make_calendar(organization, external_id="holidays")

    assert unlist_non_default_calendars() == 1
    snapshot = _reload(calendar).meta[BACKFILL_META_KEY]

    assert unlist_non_default_calendars() == 0
    assert _reload(calendar).meta[BACKFILL_META_KEY] == snapshot


def test_reverse_restores_the_previous_values(organization):
    """Reverse restores exactly what each row held -- including sync already off."""
    syncing = _make_calendar(organization, external_id="syncing", sync_enabled=True)
    opted_out = _make_calendar(organization, external_id="opted-out", sync_enabled=False)

    assert unlist_non_default_calendars() == 2
    assert restore_unlisted_calendars() == 2

    syncing = _reload(syncing)
    assert syncing.visibility == CalendarVisibility.ACTIVE
    assert syncing.sync_enabled is True
    assert BACKFILL_META_KEY not in syncing.meta

    opted_out = _reload(opted_out)
    assert opted_out.visibility == CalendarVisibility.ACTIVE
    # The user's own opt-out survives the round trip.
    assert opted_out.sync_enabled is False
    assert BACKFILL_META_KEY not in opted_out.meta


def test_reverse_ignores_rows_the_backfill_never_touched(organization):
    """Reverse only restores rows carrying the backfill snapshot."""
    default_calendar = _make_calendar(organization, external_id="primary")
    _make_default_ownership(organization, default_calendar)

    unlist_non_default_calendars()

    assert restore_unlisted_calendars() == 0
    assert _reload(default_calendar).visibility == CalendarVisibility.ACTIVE


def test_reverse_is_idempotent(organization):
    """A second reverse pass is a no-op."""
    _make_calendar(organization, external_id="holidays")

    unlist_non_default_calendars()
    assert restore_unlisted_calendars() == 1
    assert restore_unlisted_calendars() == 0
