"""Tests for ``RoomSyncAdapterResolver`` and the receiver that starts Google event sync."""

import datetime
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from model_bakery import baker
from vinta_billing.exceptions import OverLimitError

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncTriggerSource,
    CalendarType,
)
from calendar_integration.exceptions import ResourceDirectoryNotWriteEnabledError
from calendar_integration.models import (
    Calendar,
    GoogleCalendarServiceAccount,
    MicrosoftOrganizationConnection,
    ResourceCalendarProviderLink,
)
from calendar_integration.services.calendar_adapters.google_calendar_adapter import (
    GoogleCalendarAdapter,
)
from calendar_integration.services.calendar_adapters.ms_outlook_calendar_adapter import (
    MSOutlookCalendarAdapter,
)
from calendar_integration.services.room_sync_adapter_resolver import RoomSyncAdapterResolver
from calendar_integration.signals import resource_room_synced
from calendar_integration.tasks.calendar_sync_tasks import start_room_event_sync_task
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from organizations.models import Organization, OrganizationFeatureFlag


RESOLVER_MODULE = "calendar_integration.services.room_sync_adapter_resolver"
RECEIVER_MODULE = "calendar_integration.receivers.room_sync_receivers"


@pytest.fixture
def organization(db) -> Organization:
    return baker.make(Organization)


@pytest.fixture
def resolver() -> RoomSyncAdapterResolver:
    return RoomSyncAdapterResolver(microsoft_token_provider=MagicMock())


def _flag(organization: Organization, enabled: bool = True) -> None:
    OrganizationFeatureFlag.objects.create(
        organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=enabled
    )


def _google_account(organization: Organization, **kwargs) -> GoogleCalendarServiceAccount:
    return GoogleCalendarServiceAccount.objects.create(
        organization=organization,
        email="service@example.com",
        admin_email="admin@example.com",
        private_key_id="key-id",
        private_key="private-key",
        **kwargs,
    )


def _microsoft_connection(organization: Organization, **kwargs) -> MicrosoftOrganizationConnection:
    return MicrosoftOrganizationConnection.objects.create(
        organization=organization, tenant_id="tenant-1", **kwargs
    )


@pytest.mark.django_db
class TestAdapterFor:
    def test_google_uses_the_org_service_account_with_write_scope(self, organization, resolver):
        _flag(organization)
        account = _google_account(organization, write_enabled=True)
        adapter = MagicMock()
        with patch.object(
            GoogleCalendarAdapter, "from_service_account_model", return_value=adapter
        ) as build:
            result = resolver.adapter_for(organization, CalendarProvider.GOOGLE)

        assert result is adapter
        build.assert_called_once_with(account, write=True)

    def test_microsoft_uses_the_organization_connection(self, organization):
        _flag(organization)
        connection = _microsoft_connection(organization, write_enabled=True)
        token_provider = MagicMock()
        resolver = RoomSyncAdapterResolver(microsoft_token_provider=token_provider)
        adapter = MagicMock()
        with patch.object(MSOutlookCalendarAdapter, "from_app_only", return_value=adapter) as build:
            result = resolver.adapter_for(organization, CalendarProvider.MICROSOFT)

        assert result is adapter
        build.assert_called_once_with(connection, token_provider)

    @pytest.mark.parametrize("provider", [CalendarProvider.GOOGLE, CalendarProvider.MICROSOFT])
    def test_raises_without_a_write_enabled_connection(self, organization, resolver, provider):
        _flag(organization)
        _google_account(organization, write_enabled=False)
        _microsoft_connection(organization, write_enabled=False)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            resolver.adapter_for(organization, provider)

    @pytest.mark.parametrize("provider", [CalendarProvider.GOOGLE, CalendarProvider.MICROSOFT])
    def test_raises_without_any_connection(self, organization, resolver, provider):
        _flag(organization)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            resolver.adapter_for(organization, provider)

    def test_raises_for_an_unsupported_provider(self, organization, resolver):
        _flag(organization)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            resolver.adapter_for(organization, CalendarProvider.INTERNAL)

    def test_raises_when_the_flag_is_off(self, organization, resolver):
        _google_account(organization, write_enabled=True)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            resolver.adapter_for(organization, CalendarProvider.GOOGLE)

    def test_the_account_of_another_organization_is_not_used(self, organization, resolver):
        _flag(organization)
        _google_account(baker.make(Organization), write_enabled=True)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            resolver.adapter_for(organization, CalendarProvider.GOOGLE)

    def test_a_calendar_bound_service_account_is_ignored(self, organization, resolver):
        _flag(organization)
        calendar = baker.make(Calendar, organization=organization)
        _google_account(organization, write_enabled=True, calendar=calendar)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            resolver.adapter_for(organization, CalendarProvider.GOOGLE)


@pytest.mark.django_db
class TestIsWriteEnabled:
    def test_true_for_each_provider_when_flag_on_and_write_enabled(self, organization, resolver):
        _flag(organization)
        _google_account(organization, write_enabled=True)
        _microsoft_connection(organization, write_enabled=True)

        assert resolver.is_write_enabled(organization, CalendarProvider.GOOGLE) is True
        assert resolver.is_write_enabled(organization, CalendarProvider.MICROSOFT) is True

    def test_false_when_the_flag_is_off(self, organization, resolver):
        _google_account(organization, write_enabled=True)
        _microsoft_connection(organization, write_enabled=True)

        assert resolver.is_write_enabled(organization, CalendarProvider.GOOGLE) is False
        assert resolver.is_write_enabled(organization, CalendarProvider.MICROSOFT) is False

    def test_false_when_the_flag_row_is_disabled(self, organization, resolver):
        _flag(organization, enabled=False)
        _google_account(organization, write_enabled=True)

        assert resolver.is_write_enabled(organization, CalendarProvider.GOOGLE) is False

    def test_false_when_the_connection_is_not_write_enabled(self, organization, resolver):
        _flag(organization)
        _google_account(organization, write_enabled=False)

        assert resolver.is_write_enabled(organization, CalendarProvider.GOOGLE) is False
        assert resolver.is_write_enabled(organization, CalendarProvider.MICROSOFT) is False


@pytest.mark.django_db
class TestContainerWiring:
    def test_container_returns_an_adapter_for_a_write_enabled_org(self, organization, di_container):
        _flag(organization)
        _google_account(organization, write_enabled=True)
        adapter = MagicMock()

        with patch.object(
            GoogleCalendarAdapter, "from_service_account_model", return_value=adapter
        ):
            result = di_container.resource_directory_adapter_resolver().adapter_for(
                organization, CalendarProvider.GOOGLE
            )

        assert result is adapter

    def test_container_raises_for_any_other_org(self, organization, di_container):
        with pytest.raises(ResourceDirectoryNotWriteEnabledError):
            di_container.resource_directory_adapter_resolver().adapter_for(
                organization, CalendarProvider.MICROSOFT
            )


@pytest.mark.django_db
class TestRoomSyncedReceiver:
    @pytest.fixture
    def room(self, organization) -> Calendar:
        return baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
        )

    @pytest.fixture
    def delay(self):
        with patch(f"{RECEIVER_MODULE}.start_room_event_sync_task.delay") as delay:
            yield delay

    def _send(self, organization, room, provider=CalendarProvider.GOOGLE, created=True):
        # Through the real signal, so the wiring in `apps.py` is covered too.
        resource_room_synced.send(
            sender=ResourceCalendarProviderLink,
            calendar_id=room.id,
            organization_id=organization.id,
            provider=provider,
            created=created,
        )

    @pytest.mark.parametrize(
        ("provider", "created", "queued"),
        [
            (CalendarProvider.GOOGLE, True, True),
            (CalendarProvider.GOOGLE, False, False),
            (CalendarProvider.MICROSOFT, True, False),
        ],
    )
    def test_queues_the_task_for_created_google_rooms_only(
        self,
        organization,
        room,
        delay,
        django_capture_on_commit_callbacks,
        provider,
        created,
        queued,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            self._send(organization, room, provider=provider, created=created)

        if queued:
            delay.assert_called_once_with(calendar_id=room.id, organization_id=organization.id)
        else:
            delay.assert_not_called()


@pytest.mark.django_db
class TestStartRoomEventSyncTask:
    @pytest.fixture
    def room(self, organization) -> Calendar:
        return baker.make(
            Calendar,
            organization=organization,
            provider=CalendarProvider.GOOGLE,
            calendar_type=CalendarType.RESOURCE,
        )

    @pytest.fixture
    def services(self):
        calendar_service = MagicMock()
        entitlement_service = MagicMock()
        entitlement_service.is_billing_root_restricted.return_value = False
        return calendar_service, entitlement_service

    def _run(self, room, organization, services):
        calendar_service, entitlement_service = services
        start_room_event_sync_task(
            calendar_id=room.id,
            organization_id=organization.id,
            calendar_service=calendar_service,
            entitlement_service=entitlement_service,
        )

    def test_requests_a_year_of_sync_through_the_org_service_account(
        self, organization, room, services
    ):
        _flag(organization)
        account = _google_account(organization, write_enabled=True)
        before = timezone.now()

        self._run(room, organization, services)

        calendar_service = services[0]
        calendar_service.authenticate.assert_called_once_with(
            account=account, organization=organization
        )
        kwargs = calendar_service.request_calendar_sync.call_args.kwargs
        assert kwargs["calendar"] == room
        assert kwargs["should_update_events"] is True
        assert kwargs["trigger_source"] == CalendarSyncTriggerSource.IMPORT
        assert kwargs["start_datetime"] >= before
        assert kwargs["end_datetime"] - kwargs["start_datetime"] == datetime.timedelta(days=365)

    def test_skips_flag_off_organizations(self, organization, room, services):
        _google_account(organization, write_enabled=True)
        self._run(room, organization, services)
        services[0].request_calendar_sync.assert_not_called()

    def test_skips_without_a_service_account(self, organization, room, services):
        _flag(organization)
        self._run(room, organization, services)
        services[0].request_calendar_sync.assert_not_called()

    def test_skips_a_restricted_billing_root(self, organization, room, services):
        _flag(organization)
        _google_account(organization, write_enabled=True)
        services[1].is_billing_root_restricted.return_value = True

        self._run(room, organization, services)

        services[0].request_calendar_sync.assert_not_called()

    def test_a_plan_without_google_calendars_is_a_skip_not_a_failure(
        self, organization, room, services
    ):
        _flag(organization)
        _google_account(organization, write_enabled=True)
        services[0].authenticate.side_effect = OverLimitError(
            "external_calendar_google", 0, 0, "upgrade_plan"
        )

        self._run(room, organization, services)

        services[0].request_calendar_sync.assert_not_called()
