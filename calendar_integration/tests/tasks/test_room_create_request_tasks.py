"""Unit tests for ``purge_expired_resource_calendar_create_requests_task``."""

import datetime
from typing import Any

import pytest
from freezegun import freeze_time

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import Calendar, ResourceCalendarCreateRequest
from calendar_integration.tasks import purge_expired_resource_calendar_create_requests_task
from common.organization_context import organization_context
from organizations.models import Organization


NOW = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.UTC)


def _create_request(
    organization: Organization, key: str, expires_at: datetime.datetime
) -> ResourceCalendarCreateRequest:
    with organization_context(organization):
        room = Calendar.objects.create(
            organization=organization,
            name=f"Room {key}",
            provider=CalendarProvider.GOOGLE,
            external_id=f"pending-{key}",
            calendar_type=CalendarType.RESOURCE,
        )
        return ResourceCalendarCreateRequest.objects.create(
            organization=organization,
            idempotency_key=key,
            request_fingerprint="0" * 64,
            calendar=room,
            expires_at=expires_at,
        )


@pytest.fixture
def organizations(db: Any) -> tuple[Organization, Organization]:
    return (
        Organization.objects.create(name="Purge Org A"),
        Organization.objects.create(name="Purge Org B"),
    )


@freeze_time(NOW)
def test_purge_deletes_only_expired_requests_in_every_organization(
    organizations: tuple[Organization, Organization],
) -> None:
    org_a, org_b = organizations
    _create_request(org_a, "expired-a", NOW - datetime.timedelta(hours=1))
    _create_request(org_a, "at-the-boundary", NOW)
    _create_request(org_b, "expired-b", NOW - datetime.timedelta(days=3))
    _create_request(org_a, "live-a", NOW + datetime.timedelta(seconds=1))
    _create_request(org_b, "live-b", NOW + datetime.timedelta(hours=23))

    # Beat runs the task with no organization bound.
    deleted = purge_expired_resource_calendar_create_requests_task()

    assert deleted == 3
    remaining = sorted(
        ResourceCalendarCreateRequest.objects.unscoped().values_list("idempotency_key", flat=True)
    )
    assert remaining == ["live-a", "live-b"]
    # The rooms are untouched.
    assert Calendar.objects.unscoped().filter(calendar_type=CalendarType.RESOURCE).count() == 5


@freeze_time(NOW)
def test_purge_is_safe_to_run_again(organizations: tuple[Organization, Organization]) -> None:
    org_a, _ = organizations
    _create_request(org_a, "expired", NOW - datetime.timedelta(hours=1))
    _create_request(org_a, "live", NOW + datetime.timedelta(hours=1))

    assert purge_expired_resource_calendar_create_requests_task() == 1
    assert purge_expired_resource_calendar_create_requests_task() == 0
    assert list(
        ResourceCalendarCreateRequest.objects.unscoped().values_list("idempotency_key", flat=True)
    ) == ["live"]
