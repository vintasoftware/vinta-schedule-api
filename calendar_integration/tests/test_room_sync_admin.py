"""Integration tests for the room sync Django admin: links, locations, Microsoft connections."""

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

import pytest

from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.factories import (
    create_microsoft_organization_connection,
    create_resource_location,
    create_resource_provider_link,
)
from calendar_integration.models import (
    Calendar,
    MicrosoftOrganizationConnection,
    ResourceCalendarProviderLink,
)
from calendar_integration.services.microsoft_connection_service import (
    MicrosoftConnectionService,
    MicrosoftConnectionVerification,
)
from calendar_integration.tasks import push_room_to_provider_task
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.organization_context import organization_context
from organizations.models import Organization


User = get_user_model()

LINK_CHANGELIST = "admin:calendar_integration_resourcecalendarproviderlink_changelist"


@pytest.fixture
def admin_client(db: Any) -> Client:
    superuser = User.objects.create_superuser(
        email="room-sync-admin@example.com",
        password="adminpassword",  # noqa: S106
    )
    client = Client()
    client.force_login(superuser)
    return client


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Room Sync Admin Org")


@pytest.fixture
def other_organization(db: Any) -> Organization:
    return Organization.objects.create(name="Other Room Sync Admin Org")


@pytest.fixture
def resolver(di_container: Any) -> Iterator[FakeRoomDirectoryResolver]:
    fake = FakeRoomDirectoryResolver(FakeRoomDirectory())
    di_container.resource_directory_adapter_resolver.override(fake)
    try:
        yield fake
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()


@pytest.fixture
def enqueued() -> Iterator[MagicMock]:
    """The push task's ``delay``, so tests see what was queued without running it."""
    with patch.object(push_room_to_provider_task, "delay") as delay:
        yield delay


def make_link(
    organization: Organization, name: str, **link_kwargs: Any
) -> ResourceCalendarProviderLink:
    with organization_context(organization):
        room = Calendar.objects.create(
            organization=organization,
            name=name,
            external_id=f"room-{uuid.uuid4()}",
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
        )
        return create_resource_provider_link(calendar=room, **link_kwargs)


def reload(link: ResourceCalendarProviderLink) -> ResourceCalendarProviderLink:
    return ResourceCalendarProviderLink.original_manager.get(pk=link.pk)


@pytest.mark.django_db
class TestChangelistsRender:
    def test_link_changelist_lists_links_from_every_organization(
        self, admin_client: Client, organization: Organization, other_organization: Organization
    ) -> None:
        make_link(
            organization,
            "Boardroom",
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
            last_error="Provider rejected the capacity.",
        )
        make_link(other_organization, "Huddle Room")

        response = admin_client.get(reverse(LINK_CHANGELIST))

        assert response.status_code == 200
        content = response.content.decode()
        assert "Boardroom" in content
        assert "Huddle Room" in content
        assert "?sync_status__exact=sync_failed" in content
        assert "Retry sync" in content

    def test_link_change_view_renders_read_only(
        self, admin_client: Client, organization: Organization
    ) -> None:
        with organization_context(organization):
            location = create_resource_location(organization=organization, building_name="HQ")
        link = make_link(
            organization,
            "Boardroom",
            sync_status=ResourceSyncStatus.PENDING_UPDATE,
            location=location,
            provider_snapshot={"name": "Old name"},
            pending_fields={"name": "Boardroom"},
        )

        response = admin_client.get(
            reverse(
                "admin:calendar_integration_resourcecalendarproviderlink_change", args=[link.pk]
            )
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert "Old name" in content
        assert 'name="pending_fields"' not in content

    def test_location_changelist_and_change_view_render(
        self, admin_client: Client, organization: Organization
    ) -> None:
        with organization_context(organization):
            location = create_resource_location(
                organization=organization, building_name="North Tower", floor_name="7"
            )

        changelist = admin_client.get(
            reverse("admin:calendar_integration_resourcelocation_changelist")
        )
        change = admin_client.get(
            reverse("admin:calendar_integration_resourcelocation_change", args=[location.pk])
        )

        assert changelist.status_code == 200
        assert "North Tower" in changelist.content.decode()
        assert change.status_code == 200

    def test_connection_changelist_and_change_view_render(
        self, admin_client: Client, organization: Organization
    ) -> None:
        with organization_context(organization):
            connection = create_microsoft_organization_connection(
                organization=organization,
                tenant_id="11111111-2222-3333-4444-555555555555",
                consent_state="secret-nonce",
            )

        changelist = admin_client.get(
            reverse("admin:calendar_integration_microsoftorganizationconnection_changelist")
        )
        change = admin_client.get(
            reverse(
                "admin:calendar_integration_microsoftorganizationconnection_change",
                args=[connection.pk],
            )
        )

        assert changelist.status_code == 200
        assert "11111111-2222-3333-4444-555555555555" in changelist.content.decode()
        assert change.status_code == 200
        change_content = change.content.decode()
        assert 'name="tenant_id"' not in change_content
        assert "secret-nonce" not in change_content


@pytest.mark.django_db
class TestRetrySyncAction:
    def test_retry_moves_a_sync_failed_link_to_pending_and_queues_one_push(
        self,
        admin_client: Client,
        organization: Organization,
        resolver: FakeRoomDirectoryResolver,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        link = make_link(
            organization,
            "Boardroom",
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.UPDATE,
            last_error="Provider timed out.",
            attempt_count=7,
        )

        with django_capture_on_commit_callbacks(execute=True):
            response = admin_client.post(
                reverse(LINK_CHANGELIST),
                data={"action": "retry_sync", "_selected_action": [link.pk]},
            )

        assert response.status_code == 302
        link = reload(link)
        assert link.sync_status == ResourceSyncStatus.PENDING_UPDATE
        assert link.failed_operation == ""
        assert link.attempt_count == 0
        assert link.last_error == ""
        enqueued.assert_called_once_with(
            link_id=link.pk, organization_id=organization.pk, attempt_count=0
        )

    def test_retry_ignores_links_in_other_statuses(
        self,
        admin_client: Client,
        organization: Organization,
        other_organization: Organization,
        resolver: FakeRoomDirectoryResolver,
        enqueued: MagicMock,
        django_capture_on_commit_callbacks: Any,
    ) -> None:
        failed = make_link(
            other_organization,
            "Failed Room",
            sync_status=ResourceSyncStatus.SYNC_FAILED,
            failed_operation=ResourceSyncOperation.CREATE,
        )
        synced = make_link(organization, "Synced Room", sync_status=ResourceSyncStatus.SYNCED)
        pending = make_link(
            organization, "Pending Room", sync_status=ResourceSyncStatus.PENDING_DELETION
        )

        with django_capture_on_commit_callbacks(execute=True):
            response = admin_client.post(
                reverse(LINK_CHANGELIST),
                data={
                    "action": "retry_sync",
                    "_selected_action": [failed.pk, synced.pk, pending.pk],
                },
                follow=True,
            )

        assert response.status_code == 200
        assert reload(failed).sync_status == ResourceSyncStatus.PENDING_CREATION
        assert reload(synced).sync_status == ResourceSyncStatus.SYNCED
        assert reload(pending).sync_status == ResourceSyncStatus.PENDING_DELETION
        enqueued.assert_called_once_with(
            link_id=failed.pk, organization_id=other_organization.pk, attempt_count=0
        )
        messages = [str(message) for message in response.context["messages"]]
        assert messages == [
            "Queued a retry for 1 room link(s).",
            "Skipped 2 room link(s) that are not in sync failed.",
        ]


@pytest.mark.django_db
class TestVerifyAction:
    def test_verify_calls_the_service_for_each_selected_connection(
        self,
        admin_client: Client,
        organization: Organization,
        other_organization: Organization,
        di_container: Any,
    ) -> None:
        with organization_context(organization):
            connection = create_microsoft_organization_connection(organization=organization)
        with organization_context(other_organization):
            other = create_microsoft_organization_connection(organization=other_organization)

        service = MagicMock(spec=MicrosoftConnectionService)
        results = {
            organization.pk: MicrosoftConnectionVerification(
                write_enabled=True, verified_at=None, error=""
            ),
            other_organization.pk: MicrosoftConnectionVerification(
                write_enabled=False, verified_at=None, error="Missing Place.ReadWrite.All."
            ),
        }
        service.verify.side_effect = lambda org: results[org.pk]
        with di_container.microsoft_connection_service.override(service):
            response = admin_client.post(
                reverse("admin:calendar_integration_microsoftorganizationconnection_changelist"),
                data={
                    "action": "verify_connection",
                    "_selected_action": [connection.pk, other.pk],
                },
                follow=True,
            )

        assert response.status_code == 200
        verified = [call.args[0] for call in service.verify.call_args_list]
        assert sorted(org.pk for org in verified) == sorted(
            [organization.pk, other_organization.pk]
        )
        messages = {str(message) for message in response.context["messages"]}
        assert messages == {
            f"{organization}: write access verified.",
            f"{other_organization}: not write-enabled. Missing Place.ReadWrite.All.",
        }
        # The connections themselves are left to the service: the admin writes nothing.
        assert (
            MicrosoftOrganizationConnection.original_manager.filter(write_enabled=True).count() == 0
        )
