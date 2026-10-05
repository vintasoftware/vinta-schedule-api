"""Integration tests for ``CalendarService.create_synced_resource_calendar``.

The provider is ``FakeRoomDirectory``, an in-memory room directory, installed by
overriding the container's ``resource_directory_adapter_resolver``. Everything else
is real: the ``CalendarService`` and ``RoomSyncService`` come from the container,
the push runs eagerly through ``push_room_to_provider_task`` once the create's
on-commit callbacks run, and audit records are caught by patching the audit
persistence task.
"""

import datetime
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from freezegun import freeze_time
from model_bakery import baker
from vinta_billing.constants import BillingState, LimitKind
from vinta_billing.exceptions import OverLimitError
from vinta_billing.models import BillingPlan, Subscription, SubscriptionPlanLimit

from audit_integration.constants import AuditAction
from calendar_integration.constants import (
    CalendarProvider,
    CalendarType,
    CalendarVisibility,
    ResourceSyncOperation,
    ResourceSyncStatus,
)
from calendar_integration.exceptions import (
    InvalidResourceCalendarProviderError,
    InvalidResourceLocationError,
    ResourceCalendarIdempotencyKeyReusedError,
    ResourceCalendarProviderSyncNotEnabledError,
    ResourceDirectoryNotWriteEnabledError,
)
from calendar_integration.factories import create_resource_location
from calendar_integration.models import (
    Calendar,
    CalendarOwnership,
    ResourceCalendarCreateRequest,
    ResourceCalendarProviderLink,
    ResourceLocation,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.tests.room_sync_fakes import FakeRoomDirectory, FakeRoomDirectoryResolver
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag
from organizations.tests.helpers import make_admin_membership
from payments.seams.resource_keys import RESOURCE_CALENDARS
from payments.seams.scopes import scope_for
from users.factories import UserFactory
from users.models import User


NOW = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.UTC)


# ---------------------------------------------------------------------------
# Helpers and fixtures
# ---------------------------------------------------------------------------


def _enable_flag(organization: Organization) -> None:
    with organization_context(organization):
        OrganizationFeatureFlag.objects.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
        )


def _rooms(organization: Organization) -> list[Calendar]:
    return list(
        Calendar.objects.filter_by_organization(organization.id)
        .filter(calendar_type=CalendarType.RESOURCE)
        .order_by("pk")
    )


def _resource_calendar_usage(organization: Organization) -> int:
    return (
        Calendar.objects.filter_by_organization(organization.id)
        .live_of_type(CalendarType.RESOURCE)
        .count()
    )


def _audit_payloads(persist_task: MagicMock, action: str) -> list[dict]:
    """The queued audit records for ``action`` on a calendar."""
    return [
        call.args[0]
        for call in persist_task.delay.call_args_list
        if call.args[0]["action_key"] == action
        and call.args[0]["subject"]["subject_type"] == "calendar_integration.calendar"
    ]


@pytest.fixture
def organization(db: Any) -> Organization:
    organization = Organization.objects.create(name="Synced Room Org")
    _enable_flag(organization)
    return organization


@pytest.fixture
def admin(organization: Organization) -> User:
    user = UserFactory().create_user(email="room-admin@example.com")
    make_admin_membership(user=user, organization=organization)
    return user


@pytest.fixture
def bound(organization: Organization) -> Iterator[None]:
    with organization_context(organization):
        yield


@pytest.fixture
def location(organization: Organization, bound: None) -> ResourceLocation:
    return create_resource_location(
        organization=organization,
        external_building_id="hq",
        building_name="HQ",
        external_floor_id="4",
        floor_name="4",
    )


@pytest.fixture
def directory() -> FakeRoomDirectory:
    return FakeRoomDirectory(CalendarProvider.GOOGLE)


@pytest.fixture
def resolver(
    di_container: Any, directory: FakeRoomDirectory
) -> Iterator[FakeRoomDirectoryResolver]:
    resolver = FakeRoomDirectoryResolver(directory)
    di_container.resource_directory_adapter_resolver.override(resolver)
    try:
        yield resolver
    finally:
        di_container.resource_directory_adapter_resolver.reset_override()


@pytest.fixture
def service(
    di_container: Any, organization: Organization, admin: User, resolver: Any
) -> CalendarService:
    service = di_container.calendar_service()
    service.initialize_without_provider(user_or_token=admin, organization=organization)
    return service


@pytest.fixture
def persist_audit() -> Iterator[MagicMock]:
    with patch("vinta_audit_logs.tasks.persist_audit_record") as persist_task:
        yield persist_task


def _create(service: CalendarService, location: ResourceLocation, **overrides: Any) -> Calendar:
    kwargs: dict[str, Any] = {
        "provider": CalendarProvider.GOOGLE,
        "location_id": location.id,
        "name": "Conf Room 4B",
        "description": "Fourth floor",
        "capacity": 8,
        "idempotency_key": "K1",
    }
    kwargs.update(overrides)
    return service.create_synced_resource_calendar(**kwargs)


# ---------------------------------------------------------------------------
# Spec acceptance scenarios 1-3
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("bound")
class TestAcceptance:
    def test_room_is_pending_creation_then_synced_after_the_push(
        self,
        service: CalendarService,
        location: ResourceLocation,
        directory: FakeRoomDirectory,
        organization: Organization,
        admin: User,
        persist_audit: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            room = _create(service, location)

            # Accepted right away: pending creation, not bookable, nothing on the provider.
            link = ResourceCalendarProviderLink.objects.get(calendar=room)
            assert link.sync_status == ResourceSyncStatus.PENDING_CREATION
            assert link.is_bookable is False
            assert link.location == location
            assert link.provider == CalendarProvider.GOOGLE
            assert link.pending_fields == {
                "name": "Conf Room 4B",
                "description": "Fourth floor",
                "capacity": 8,
                "location_ref": {"external_building_id": "hq", "external_floor_id": "4"},
            }
            assert link.retry_deadline is not None
            assert directory.calls == []

        room.refresh_from_db()
        assert room.provider == CalendarProvider.GOOGLE
        assert room.calendar_type == CalendarType.RESOURCE
        assert room.visibility == CalendarVisibility.ACTIVE
        assert room.external_id == f"pending-{link.provisional_key}"
        assert (room.name, room.description, room.capacity) == ("Conf Room 4B", "Fourth floor", 8)
        ownerships = CalendarOwnership.objects.filter_by_organization(organization.id).filter(
            calendar=room
        )
        assert [o.membership_user_id for o in ownerships] == [admin.id]
        create_request = ResourceCalendarCreateRequest.objects.get(idempotency_key="K1")
        assert create_request.calendar == room

        # Commit: the audit record goes out and the push runs (eagerly).
        for callback in callbacks:
            callback()

        [created] = _audit_payloads(persist_audit, AuditAction.CREATE)
        assert created["subject"]["subject_id"] == str(room.id)
        assert created["subject"]["subject_label"] == "Conf Room 4B"
        assert created["scope"]["scope_key"] == str(organization.id)

        assert directory.calls == ["create_room"]
        provider_room = directory.rooms[f"vinta-{link.provisional_key}"]
        assert (provider_room.name, provider_room.capacity) == ("Conf Room 4B", 8)
        link.refresh_from_db()
        room.refresh_from_db()
        assert link.sync_status == ResourceSyncStatus.SYNCED
        assert link.is_bookable is True
        assert link.pending_fields == {}
        assert room.external_id == provider_room.external_id
        assert room.email == provider_room.email

    def test_replay_with_the_same_key_returns_the_same_room(
        self,
        service: CalendarService,
        location: ResourceLocation,
        directory: FakeRoomDirectory,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            first = _create(service, location)
        with django_capture_on_commit_callbacks(execute=True) as replay_callbacks:
            second = _create(service, location)

        assert second.id == first.id
        assert _rooms(organization) == [first]
        assert ResourceCalendarProviderLink.objects.count() == 1
        assert ResourceCalendarCreateRequest.objects.count() == 1
        # Nothing queued for the replay, and the provider holds exactly one room.
        assert replay_callbacks == []
        assert directory.calls == ["create_room"]
        assert len(directory.rooms) == 1

    def test_not_write_enabled_is_rejected_with_nothing_created(
        self,
        service: CalendarService,
        organization: Organization,
        resolver: FakeRoomDirectoryResolver,
    ) -> None:
        microsoft_location = create_resource_location(
            organization=organization, provider=CalendarProvider.MICROSOFT
        )
        resolver.write_enabled = False
        usage_before = _resource_calendar_usage(organization)

        with pytest.raises(ResourceDirectoryNotWriteEnabledError) as exc_info:
            _create(service, microsoft_location, provider=CalendarProvider.MICROSOFT)

        assert str(exc_info.value) == (
            "Microsoft 365 write access is not enabled for this organization."
        )
        assert _rooms(organization) == []
        assert ResourceCalendarProviderLink.objects.count() == 0
        assert ResourceCalendarCreateRequest.objects.count() == 0
        assert _resource_calendar_usage(organization) == usage_before


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("bound")
class TestValidation:
    def test_inactive_location_is_rejected(
        self, service: CalendarService, location: ResourceLocation, organization: Organization
    ) -> None:
        location.is_active = False
        location.save(update_fields=["is_active"])

        with pytest.raises(InvalidResourceLocationError):
            _create(service, location)

        assert _rooms(organization) == []

    def test_location_of_another_provider_is_rejected(
        self, service: CalendarService, organization: Organization
    ) -> None:
        microsoft_location = create_resource_location(
            organization=organization, provider=CalendarProvider.MICROSOFT
        )

        with pytest.raises(InvalidResourceLocationError):
            _create(service, microsoft_location, provider=CalendarProvider.GOOGLE)

        assert _rooms(organization) == []

    def test_location_of_another_organization_is_rejected(
        self, service: CalendarService, organization: Organization
    ) -> None:
        other = Organization.objects.create(name="Other Org")
        with organization_context(other):
            foreign_location = create_resource_location(organization=other)

        with pytest.raises(InvalidResourceLocationError):
            _create(service, foreign_location)

        assert _rooms(organization) == []

    def test_missing_location_is_rejected(
        self, service: CalendarService, location: ResourceLocation, organization: Organization
    ) -> None:
        with pytest.raises(InvalidResourceLocationError) as exc_info:
            _create(service, location, location_id=None)

        assert str(exc_info.value) == "A location is required to create a room."
        assert _rooms(organization) == []

    @pytest.mark.parametrize(
        "provider", [CalendarProvider.INTERNAL, CalendarProvider.APPLE, CalendarProvider.ICS]
    )
    def test_provider_without_a_room_directory_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        provider: str,
    ) -> None:
        with pytest.raises(InvalidResourceCalendarProviderError):
            _create(service, location, provider=provider)

        assert _rooms(organization) == []

    def test_key_reused_with_a_different_payload_is_rejected(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            first = _create(service, location)

        with pytest.raises(ResourceCalendarIdempotencyKeyReusedError):
            _create(service, location, capacity=12)

        assert _rooms(organization) == [first]

    def test_an_expired_key_can_be_used_again(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        with freeze_time(NOW), django_capture_on_commit_callbacks(execute=True):
            first = _create(service, location)

        with (
            freeze_time(NOW + datetime.timedelta(hours=25)),
            django_capture_on_commit_callbacks(execute=True),
        ):
            second = _create(service, location, capacity=12)

        assert second.id != first.id
        assert _rooms(organization) == [first, second]
        [create_request] = ResourceCalendarCreateRequest.objects.all()
        assert create_request.calendar == second

    def test_creates_without_a_key_are_not_deduplicated(
        self,
        service: CalendarService,
        location: ResourceLocation,
        organization: Organization,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            first = _create(service, location, idempotency_key=None)
            second = _create(service, location, idempotency_key=None)

        assert _rooms(organization) == [first, second]
        assert ResourceCalendarCreateRequest.objects.count() == 0


# ---------------------------------------------------------------------------
# Plan limit
# ---------------------------------------------------------------------------


@pytest.mark.no_auto_subscription
@pytest.mark.django_db
def test_over_the_limit_is_rejected_with_nothing_created(
    di_container: Any, resolver: FakeRoomDirectoryResolver
) -> None:
    organization = baker.make(Organization, parent=None, can_invite_organizations=False)
    _enable_flag(organization)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        scope=scope_for(organization),
        plan=baker.make(BillingPlan, is_default_for_new_scopes=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionPlanLimit,
        subscription=subscription,
        resource_key=RESOURCE_CALENDARS,
        limit_value=1,
        kind=LimitKind.PREPAID,
    )
    baker.make(
        Calendar,
        organization=organization,
        calendar_type=CalendarType.RESOURCE,
        external_id="seed-room",
    )
    service = di_container.calendar_service()
    service.initialize_without_provider(organization=organization)

    with organization_context(organization):
        location = create_resource_location(organization=organization)
        with pytest.raises(OverLimitError) as exc_info:
            _create(service, location)

        assert exc_info.value.resource_key == RESOURCE_CALENDARS
        assert exc_info.value.current_usage == 1
        assert [room.external_id for room in _rooms(organization)] == ["seed-room"]
        assert ResourceCalendarProviderLink.objects.count() == 0
        assert ResourceCalendarCreateRequest.objects.count() == 0


# ---------------------------------------------------------------------------
# Feature flag off
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestFlagOff:
    @pytest.fixture
    def organization(self, db: Any) -> Organization:
        return Organization.objects.create(name="Flag Off Org")

    def test_provider_room_is_rejected(
        self,
        service: CalendarService,
        organization: Organization,
        directory: FakeRoomDirectory,
    ) -> None:
        with organization_context(organization):
            location = create_resource_location(organization=organization)

            with pytest.raises(ResourceCalendarProviderSyncNotEnabledError) as exc_info:
                _create(service, location)

            assert str(exc_info.value) == (
                "Resource calendar provider sync is not enabled for this organization."
            )
            assert _rooms(organization) == []
        assert directory.calls == []

    def test_manual_room_create_is_unchanged(
        self,
        service: CalendarService,
        organization: Organization,
        admin: User,
        persist_audit: MagicMock,
        django_capture_on_commit_callbacks: Callable[..., Any],
    ) -> None:
        with (
            organization_context(organization),
            django_capture_on_commit_callbacks(execute=True),
        ):
            room = service.create_resource_calendar(
                name="Manual Room", description="Ground floor", capacity=4
            )

        with organization_context(organization):
            room.refresh_from_db()
            assert room.provider == CalendarProvider.INTERNAL
            assert room.external_id == ""
            assert not ResourceCalendarProviderLink.objects.filter(calendar=room).exists()
        [created] = _audit_payloads(persist_audit, AuditAction.CREATE)
        assert created["subject"]["subject_id"] == str(room.id)
        assert created["subject"]["subject_label"] == "Manual Room"


def test_audit_payload_records_one_create_per_room(
    service: CalendarService,
    location: ResourceLocation,
    persist_audit: MagicMock,
    bound: None,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    """The create records one ``CREATE`` for the room and, after the push, one sync record."""
    with django_capture_on_commit_callbacks(execute=True):
        room = _create(service, location)

    assert [
        p["subject"]["subject_id"] for p in _audit_payloads(persist_audit, AuditAction.CREATE)
    ] == [str(room.id)]
    [synced] = _audit_payloads(persist_audit, AuditAction.ROOM_PROVIDER_SYNCED)
    assert synced["diff"]["operation"] == ResourceSyncOperation.CREATE
