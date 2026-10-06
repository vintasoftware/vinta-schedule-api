"""Integration tests for the hourly room resync tasks.

The tasks resolve ``room_resync_service`` from the real DI container, with only
``resource_directory_adapter_resolver`` overridden by an in-memory directory, so
the wiring in ``di_core/containers.py`` is under test too.
"""

import threading
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import call, patch

from django.db import connection, connections, transaction
from django.forms.models import model_to_dict
from django.test.utils import CaptureQueriesContext

import pytest
from celery import Task
from dependency_injector import providers

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import ResourceDirectoryError
from calendar_integration.factories import create_resource_location, create_resource_provider_link
from calendar_integration.models import Calendar, ResourceCalendarProviderLink, ResourceLocation
from calendar_integration.tasks import (
    resync_organization_rooms_task,
    resync_rooms_for_flagged_organizations_task,
)
from calendar_integration.tests.room_resync_fakes import (
    FakeAdapterResolver,
    FakeRoomDirectory,
    make_location,
    make_room,
)
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag


GOOGLE = CalendarProvider.GOOGLE


@pytest.fixture
def resolver(di_container: Any) -> Iterator[FakeAdapterResolver]:
    resolver = FakeAdapterResolver()
    with di_container.resource_directory_adapter_resolver.override(providers.Object(resolver)):
        yield resolver


def _organization(name: str, *, flag_on: bool) -> Organization:
    organization = Organization.objects.create(name=name)
    OrganizationFeatureFlag.objects.create(
        organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=flag_on
    )
    return organization


def _synced_room(organization: Organization, external_id: str, name: str) -> Calendar:
    with organization_context(organization):
        location = create_resource_location(organization=organization)
        calendar = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=external_id,
            email=f"{external_id}@resource.example.com",
            provider=GOOGLE,
            calendar_type=CalendarType.RESOURCE,
            capacity=8,
        )
        create_resource_provider_link(
            calendar=calendar,
            location=location,
            provider_snapshot=make_room(external_id, name).synced_values(),
        )
    return calendar


def _rows(organization: Organization) -> dict[str, list[dict[str, Any]]]:
    """Every room, link and location row of ``organization``, field by field."""
    return {
        "calendars": [
            model_to_dict(row)
            for row in Calendar.objects.filter_by_organization(organization.id).order_by("id")
        ],
        "links": [
            model_to_dict(row)
            for row in ResourceCalendarProviderLink.objects.filter_by_organization(
                organization.id
            ).order_by("id")
        ],
        "locations": [
            model_to_dict(row)
            for row in ResourceLocation.objects.filter_by_organization(organization.id).order_by(
                "id"
            )
        ],
    }


def _directory(resolver: FakeAdapterResolver, organization: Organization) -> FakeRoomDirectory:
    directory = FakeRoomDirectory(provider=GOOGLE, locations=[make_location()])
    resolver.enable(organization, directory)
    return directory


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_fan_out_enqueues_every_provider_of_flag_on_organizations_only() -> None:
    flag_on = _organization("On", flag_on=True)
    _organization("Off", flag_on=False)
    Organization.objects.create(name="No flag row")

    with patch.object(resync_organization_rooms_task, "delay") as delay:
        resync_rooms_for_flagged_organizations_task()

    assert delay.call_args_list == [
        call(organization_id=flag_on.id, provider=CalendarProvider.GOOGLE),
        call(organization_id=flag_on.id, provider=CalendarProvider.MICROSOFT),
    ]


@pytest.mark.django_db
def test_fan_out_resyncs_flag_on_organizations_and_leaves_flag_off_ones_byte_for_byte(
    resolver: FakeAdapterResolver,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    flag_on = _organization("On", flag_on=True)
    flag_off = _organization("Off", flag_on=False)
    on_room = _synced_room(flag_on, "on-room", "Huddle 1")
    _synced_room(flag_off, "off-room", "Huddle 1")
    _directory(resolver, flag_on).rooms = [make_room("on-room", "Huddle One")]
    # Even a write-enabled directory with changes is not read for a flag-off org.
    off_directory = _directory(resolver, flag_off)
    off_directory.rooms = [make_room("off-room", "Renamed"), make_room("off-new", "New")]
    before = _rows(flag_off)

    # Eager in tests: each `.delay` runs the per-organization task inline.
    with django_capture_on_commit_callbacks(execute=True):
        resync_rooms_for_flagged_organizations_task()

    with organization_context(flag_on):
        on_room.refresh_from_db()
    assert on_room.name == "Huddle One"
    assert off_directory.list_calls == 0
    assert _rows(flag_off) == before


# ---------------------------------------------------------------------------
# Per-organization task
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_task_is_a_no_op_when_the_flag_was_turned_off_after_the_fan_out(
    resolver: FakeAdapterResolver,
) -> None:
    organization = _organization("Flipped", flag_on=False)
    directory = _directory(resolver, organization)
    directory.rooms = [make_room("room-1", "Huddle 1")]

    resync_organization_rooms_task(organization_id=organization.id, provider=GOOGLE)

    assert directory.list_calls == 0
    assert not Calendar.objects.filter_by_organization(organization.id).exists()


@pytest.mark.django_db
def test_task_is_a_no_op_for_a_missing_organization(resolver: FakeAdapterResolver) -> None:
    resync_organization_rooms_task(organization_id=987654, provider=GOOGLE)


@pytest.mark.django_db
def test_task_logs_and_drops_a_provider_failure(
    resolver: FakeAdapterResolver, caplog: pytest.LogCaptureFixture
) -> None:
    organization = _organization("Broken", flag_on=True)
    directory = _directory(resolver, organization)

    with patch.object(directory, "list_rooms", side_effect=ResourceDirectoryError("boom")):
        resync_organization_rooms_task(organization_id=organization.id, provider=GOOGLE)

    assert not ResourceLocation.objects.filter_by_organization(organization.id).exists()
    assert (
        f"Room resync for organization {organization.id} on google failed: "
        "ResourceDirectoryError (transient=True)"
    ) in caplog.text


def test_task_delay_arguments_pass_celerys_signature_check() -> None:
    # The check `Task.apply_async` runs before sending, as in `test_task_signatures.py`.
    task: Task = resync_organization_rooms_task
    task.__header__(organization_id=1, provider=GOOGLE)  # type: ignore[attr-defined]
    fan_out: Task = resync_rooms_for_flagged_organizations_task
    fan_out.__header__()  # type: ignore[attr-defined]


@pytest.mark.django_db
def test_rerun_with_no_provider_change_writes_nothing(
    resolver: FakeAdapterResolver,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    organization = _organization("Steady", flag_on=True)
    _synced_room(organization, "room-1", "Huddle 1")
    directory = _directory(resolver, organization)
    directory.rooms = [make_room("room-1", "Huddle 1"), make_room("room-2", "Huddle 2")]
    with django_capture_on_commit_callbacks(execute=True):
        resync_organization_rooms_task(organization_id=organization.id, provider=GOOGLE)
    before = _rows(organization)
    assert len(before["calendars"]) == 2

    with (
        CaptureQueriesContext(connection) as queries,
        django_capture_on_commit_callbacks(execute=True) as callbacks,
    ):
        resync_organization_rooms_task(organization_id=organization.id, provider=GOOGLE)

    writes = [
        query["sql"]
        for query in queries.captured_queries
        if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
    ]
    # Stamping the locations it saw is the one write an unchanged run makes.
    assert len(writes) == 1
    assert writes[0].startswith('UPDATE "calendar_integration_resourcelocation" SET "last_seen_at"')
    assert callbacks == []
    after = _rows(organization)
    for row in after["locations"] + before["locations"]:
        row.pop("last_seen_at")
    assert after == before


@pytest.mark.django_db(transaction=True)
def test_link_locked_by_a_concurrent_push_is_skipped(resolver: FakeAdapterResolver) -> None:
    organization = _organization("Busy", flag_on=True)
    calendar = _synced_room(organization, "room-1", "Huddle 1")
    directory = _directory(resolver, organization)
    directory.rooms = [make_room("room-1", "Renamed on the provider")]
    with organization_context(organization):
        link_id = ResourceCalendarProviderLink.objects.get(calendar=calendar).id

    locked = threading.Event()
    release = threading.Event()

    def hold_the_push_lock() -> None:
        # What the push engine does while it talks to the provider.
        try:
            with organization_context(organization), transaction.atomic():
                ResourceCalendarProviderLink.objects.locked_for_update(link_id).get()
                locked.set()
                release.wait(timeout=30)
        finally:
            connections.close_all()

    pusher = threading.Thread(target=hold_the_push_lock)
    pusher.start()
    try:
        assert locked.wait(timeout=30)
        resync_organization_rooms_task(organization_id=organization.id, provider=GOOGLE)
        with organization_context(organization):
            calendar.refresh_from_db()
            assert calendar.name == "Huddle 1"
    finally:
        release.set()
        pusher.join(timeout=30)

    # Once the push lets go, the next hourly run picks the change up.
    resync_organization_rooms_task(organization_id=organization.id, provider=GOOGLE)
    with organization_context(organization):
        calendar.refresh_from_db()
        link = ResourceCalendarProviderLink.objects.get(id=link_id)
    assert calendar.name == "Renamed on the provider"
    assert calendar.visibility == CalendarVisibility.ACTIVE
    assert link.sync_status == ResourceSyncStatus.SYNCED
