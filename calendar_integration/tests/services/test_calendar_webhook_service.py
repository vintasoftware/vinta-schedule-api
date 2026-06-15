"""Unit tests for CalendarWebhookService.

Tests construct CalendarWebhookService directly (bypassing the CalendarService
facade) using a real CalendarServiceContext fed a fake calendar adapter, plus a
lightweight fake host for the concerns routed back to the facade
(``request_calendar_sync``, ``request_webhook_triggered_sync``,
``_get_calendar_adapter_cls_for_provider``, ``_get_write_adapter_for_calendar``,
``_get_calendar_by_external_id``).

The flows covered are:
- subscription create (adapter returns subscription data -> CalendarWebhookSubscription
  is persisted with correct fields);
- subscription refresh (extend expiration by provider-specific duration);
- subscription delete (mark is_active=False);
- process_webhook_notification (static adapter path -> CalendarWebhookEvent is created
  and request_webhook_triggered_sync is called through the host);
- get_webhook_health_status (counts subscriptions and events in last 24 hours).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from allauth.socialaccount.models import SocialAccount

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncTriggerSource,
    IncomingWebhookProcessingStatus,
)
from calendar_integration.exceptions import (
    CalendarServiceOrganizationNotSetError,
    ServiceNotAuthenticatedError,
)
from calendar_integration.models import (
    Calendar,
    CalendarSync,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
)
from calendar_integration.services.calendar_service_context import CalendarServiceContext
from calendar_integration.services.calendar_webhook_service import (
    CalendarWebhookService,
    WebhookHealthStatus,
)
from organizations.models import Organization
from users.models import Profile, User


# ---------------------------------------------------------------------------
# Fake host
# ---------------------------------------------------------------------------


class FakeHost:
    """Minimal WebhookServiceHost used in unit tests.

    Records the calls routed back to the facade so individual tests can assert on
    them. ``request_calendar_sync`` and ``request_webhook_triggered_sync`` return
    configurable values; adapter helpers proxy to a fake adapter or raise as needed.
    """

    def __init__(self, fake_adapter: Any | None = None) -> None:
        self.request_calendar_sync_calls: list[dict[str, Any]] = []
        self.request_webhook_triggered_sync_calls: list[tuple[str, Any]] = []
        self._fake_adapter = fake_adapter
        # If set, _get_calendar_by_external_id returns this calendar.
        self.calendar_by_external_id: Calendar | None = None
        # CalendarSync to return from request_calendar_sync (None by default)
        self.calendar_sync_return: CalendarSync | None = None
        # CalendarSync to return from request_webhook_triggered_sync
        self.webhook_triggered_sync_return: CalendarSync | None = None

    def request_calendar_sync(
        self,
        calendar: Calendar,
        start_datetime: datetime.datetime,
        end_datetime: datetime.datetime,
        should_update_events: bool = False,
        trigger_source: CalendarSyncTriggerSource = CalendarSyncTriggerSource.MANUAL,
    ) -> CalendarSync | None:
        self.request_calendar_sync_calls.append(
            {
                "calendar": calendar,
                "start_datetime": start_datetime,
                "end_datetime": end_datetime,
                "should_update_events": should_update_events,
                "trigger_source": trigger_source,
            }
        )
        return self.calendar_sync_return

    def request_webhook_triggered_sync(
        self,
        external_calendar_id: str,
        webhook_event: CalendarWebhookEvent,
        sync_window_hours: int = 24,
    ) -> CalendarSync | None:
        self.request_webhook_triggered_sync_calls.append((external_calendar_id, webhook_event))
        return self.webhook_triggered_sync_return

    def _get_calendar_adapter_cls_for_provider(self, provider: CalendarProvider) -> type:
        # Return a class with the static method we need.
        if self._fake_adapter is not None:
            adapter_cls = MagicMock()
            adapter_cls.validate_webhook_notification_static = (
                self._fake_adapter.validate_webhook_notification_static
            )
            adapter_cls.parse_webhook_headers = self._fake_adapter.parse_webhook_headers
            adapter_cls.extract_calendar_external_id_from_webhook_request = (
                self._fake_adapter.extract_calendar_external_id_from_webhook_request
            )
            return adapter_cls
        raise NotImplementedError("No fake adapter configured")

    def _get_write_adapter_for_calendar(self, calendar: Calendar) -> Any | None:
        return None  # Force static validation path in most tests

    def _get_calendar_by_external_id(self, calendar_external_id: str) -> Calendar:
        if self.calendar_by_external_id is not None:
            return self.calendar_by_external_id
        raise Calendar.DoesNotExist(calendar_external_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def organization(db: Any) -> Organization:
    return Organization.objects.create(name="Webhook Test Org")


@pytest.fixture
def user(db: Any) -> User:
    u = User.objects.create_user(email="test_webhook@example.com", password="pass")  # noqa: S106
    Profile.objects.create(user=u)
    return u


@pytest.fixture
def social_account(db: Any, user: User) -> SocialAccount:
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="wh-999")


@pytest.fixture
def calendar(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Webhook Calendar",
        external_id="wh_cal_001",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def fake_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.provider = CalendarProvider.GOOGLE
    return adapter


@pytest.fixture
def context(
    organization: Organization, user: User, fake_adapter: MagicMock
) -> CalendarServiceContext:
    return CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=user,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


@pytest.fixture
def unauthenticated_context(organization: Organization, user: User) -> CalendarServiceContext:
    """Context without a calendar adapter (initialize_without_provider state)."""
    return CalendarServiceContext(
        organization=organization,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


def make_service(context: CalendarServiceContext, host: FakeHost) -> CalendarWebhookService:
    return CalendarWebhookService(context=context, calendar_cache={}, host=host)


# ---------------------------------------------------------------------------
# Tests: subscription create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_calendar_webhook_subscription_google(
    context: CalendarServiceContext,
    calendar: Calendar,
    fake_adapter: MagicMock,
) -> None:
    """create_calendar_webhook_subscription persists a subscription row with
    the correct fields when the provider returns a Google-style response."""
    fake_adapter.create_webhook_subscription_with_tracking.return_value = {
        "channel_id": "channel-abc",
        "resource_id": "resource-abc",
        "resource_uri": "https://www.googleapis.com/calendar/v3/calendars/wh_cal_001/events",
        "expiration": "1700000000000",  # milliseconds since epoch
        "calendar_id": "wh_cal_001",
        "callback_url": "https://example.com/webhook",
        "channel_token": "token-xyz",
    }

    host = FakeHost(fake_adapter=fake_adapter)
    service = make_service(context, host)

    sub = service.create_calendar_webhook_subscription(
        calendar=calendar,
        callback_url="https://example.com/webhook",
        expiration_hours=24,
    )

    assert sub.calendar == calendar
    assert sub.provider == CalendarProvider.GOOGLE
    assert sub.channel_id == "channel-abc"
    assert sub.external_resource_id == "resource-abc"
    assert sub.callback_url == "https://example.com/webhook"
    assert sub.is_active is True
    # Expiration should be parsed from milliseconds
    assert sub.expires_at is not None
    assert sub.expires_at == datetime.datetime.fromtimestamp(1700000000000 / 1000, tz=datetime.UTC)

    # Verify it's persisted (must filter by organization per multi-tenancy contract)
    persisted = CalendarWebhookSubscription.objects.get(
        id=sub.id, organization=calendar.organization_id
    )
    assert persisted.channel_id == "channel-abc"


@pytest.mark.django_db
def test_create_calendar_webhook_subscription_requires_auth(
    unauthenticated_context: CalendarServiceContext,
    calendar: Calendar,
) -> None:
    """create_calendar_webhook_subscription raises ServiceNotAuthenticatedError when
    not authenticated (is_authenticated_calendar_service raises before the local
    ValueError guard because raise_error=True by default)."""
    host = FakeHost()
    service = make_service(unauthenticated_context, host)

    with pytest.raises(ServiceNotAuthenticatedError, match="Calendar service is not authenticated"):
        service.create_calendar_webhook_subscription(
            calendar=calendar,
            callback_url="https://example.com/webhook",
        )


# ---------------------------------------------------------------------------
# Tests: subscription refresh
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_refresh_webhook_subscription_google(
    context: CalendarServiceContext,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """refresh_webhook_subscription extends expires_at by 7 days for Google."""
    sub = CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="ext-sub-1",
        channel_id="ch-1",
        callback_url="https://example.com/wh",
        expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=2),
    )

    host = FakeHost()
    service = make_service(context, host)
    result = service.refresh_webhook_subscription(subscription_id=sub.id)

    assert result is not None
    assert result.id == sub.id
    # Should be approximately 7 days from now
    expected_min = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=6)
    expected_max = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(days=8)
    assert expected_min <= result.expires_at <= expected_max


@pytest.mark.django_db
def test_refresh_webhook_subscription_not_found_returns_none(
    context: CalendarServiceContext,
    organization: Organization,
) -> None:
    """refresh_webhook_subscription returns None when the subscription doesn't exist."""
    host = FakeHost()
    service = make_service(context, host)
    result = service.refresh_webhook_subscription(subscription_id=99999)
    assert result is None


# ---------------------------------------------------------------------------
# Tests: subscription delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_webhook_subscription(
    context: CalendarServiceContext,
    calendar: Calendar,
    organization: Organization,
) -> None:
    """delete_webhook_subscription marks the subscription as inactive."""
    sub = CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="ext-sub-del",
        channel_id="ch-del",
        callback_url="https://example.com/wh",
    )
    assert sub.is_active is True

    host = FakeHost()
    service = make_service(context, host)
    result = service.delete_webhook_subscription(subscription_id=sub.id)

    assert result is True
    sub.refresh_from_db()
    assert sub.is_active is False


@pytest.mark.django_db
def test_delete_webhook_subscription_not_found(
    context: CalendarServiceContext,
) -> None:
    """delete_webhook_subscription returns False when the subscription doesn't exist."""
    host = FakeHost()
    service = make_service(context, host)
    result = service.delete_webhook_subscription(subscription_id=99999)
    assert result is False


# ---------------------------------------------------------------------------
# Tests: process_webhook_notification (static-adapter path)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_process_webhook_notification_creates_event_and_calls_sync(
    context: CalendarServiceContext,
    organization: Organization,
    calendar: Calendar,
    fake_adapter: MagicMock,
) -> None:
    """process_webhook_notification creates a CalendarWebhookEvent and calls
    request_webhook_triggered_sync through the host when authenticated."""
    # Configure fake adapter static validation to return known parsed data
    fake_adapter.validate_webhook_notification_static.return_value = {
        "provider": "google",
        "calendar_id": "wh_cal_001",
        "event_id": "evt_001",
        "event_type": "exists",
        "resource_id": "res-001",
        "channel_id": "ch-001",
    }

    # Create a fake CalendarSync to return from the host
    calendar_sync = CalendarSync.objects.create(
        calendar=calendar,
        organization=organization,
        start_datetime=datetime.datetime.now(tz=datetime.UTC),
        end_datetime=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=24),
        should_update_events=True,
    )

    host = FakeHost(fake_adapter=fake_adapter)
    host.webhook_triggered_sync_return = calendar_sync
    service = make_service(context, host)

    headers = {
        "X-Goog-Resource-ID": "res-001",
        "X-Goog-Resource-URI": (
            "https://www.googleapis.com/calendar/v3/calendars/wh_cal_001/events"
        ),
        "X-Goog-Resource-State": "exists",
        "X-Goog-Channel-ID": "ch-001",
        "X-Goog-Channel-Token": "tok",
    }

    result = service.process_webhook_notification(
        provider="google",
        calendar_external_id="wh_cal_001",
        headers=headers,
    )

    assert result is not None
    assert isinstance(result, CalendarWebhookEvent)
    assert result.provider == "google"
    assert result.event_type == "exists"
    assert result.external_calendar_id == "wh_cal_001"
    assert result.organization_id == organization.id

    # Host should have been called with the correct external_calendar_id
    assert len(host.request_webhook_triggered_sync_calls) == 1
    called_ext_id, called_event = host.request_webhook_triggered_sync_calls[0]
    assert called_ext_id == "wh_cal_001"
    assert called_event.id == result.id


@pytest.mark.django_db
def test_process_webhook_notification_no_sync_marks_ignored(
    context: CalendarServiceContext,
    organization: Organization,
    fake_adapter: MagicMock,
) -> None:
    """process_webhook_notification marks the event IGNORED when
    request_webhook_triggered_sync returns None."""
    fake_adapter.validate_webhook_notification_static.return_value = {
        "provider": "google",
        "calendar_id": "wh_cal_001",
        "event_id": "",
        "event_type": "exists",
        "resource_id": "res-001",
        "channel_id": "ch-001",
    }

    host = FakeHost(fake_adapter=fake_adapter)
    host.webhook_triggered_sync_return = None  # Sync not triggered
    service = make_service(context, host)

    headers = {
        "X-Goog-Resource-ID": "res-001",
        "X-Goog-Resource-URI": (
            "https://www.googleapis.com/calendar/v3/calendars/wh_cal_001/events"
        ),
        "X-Goog-Resource-State": "exists",
        "X-Goog-Channel-ID": "ch-001",
        "X-Goog-Channel-Token": "tok",
    }

    result = service.process_webhook_notification(
        provider="google",
        calendar_external_id="wh_cal_001",
        headers=headers,
    )

    assert result is not None
    result.refresh_from_db()
    assert result.processing_status == IncomingWebhookProcessingStatus.IGNORED


@pytest.mark.django_db
def test_process_webhook_notification_requires_organization_raises_immediately(
    user: User,
    fake_adapter: MagicMock,
) -> None:
    """process_webhook_notification raises CalendarServiceOrganizationNotSetError
    immediately when the organization is unset, before any calendar lookup or
    webhook validation runs."""
    context_no_org = CalendarServiceContext(
        organization=None,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )
    host = FakeHost(fake_adapter=fake_adapter)
    service = make_service(context_no_org, host)

    with pytest.raises(CalendarServiceOrganizationNotSetError):
        service.process_webhook_notification(
            provider="google",
            calendar_external_id="wh_cal_001",
            headers={},
        )

    # Guard must short-circuit before any calendar lookup or validation occurs.
    fake_adapter.validate_webhook_notification_static.assert_not_called()
    assert host.request_webhook_triggered_sync_calls == []


# ---------------------------------------------------------------------------
# Tests: get_webhook_health_status
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_get_webhook_health_status_empty(
    context: CalendarServiceContext,
    organization: Organization,
) -> None:
    """get_webhook_health_status returns 100% success rate with no events or subscriptions."""
    host = FakeHost()
    service = make_service(context, host)
    status: WebhookHealthStatus = service.get_webhook_health_status()

    assert status["total_subscriptions"] == 0
    assert status["active_subscriptions"] == 0
    assert status["expired_subscriptions"] == 0
    assert status["expiring_soon_subscriptions"] == 0
    assert status["recent_events_count"] == 0
    assert status["failed_events_count"] == 0
    assert status["success_rate"] == 100.0


@pytest.mark.django_db
def test_get_webhook_health_status_with_data(
    context: CalendarServiceContext,
    organization: Organization,
    calendar: Calendar,
) -> None:
    """get_webhook_health_status counts subscriptions and recent events correctly."""
    now = datetime.datetime.now(tz=datetime.UTC)

    # A second calendar for the expired subscription (unique_together constraint is
    # (organization, calendar, provider), so two subs for the same (org, calendar, provider)
    # would violate it).
    calendar_b = Calendar.objects.create(
        name="Webhook Calendar B",
        external_id="wh_cal_002",
        provider=CalendarProvider.MICROSOFT,
        organization=organization,
    )

    # One active subscription expiring soon (within 24h)
    CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="sub-1",
        channel_id="ch-1",
        callback_url="https://example.com/wh",
        expires_at=now + datetime.timedelta(hours=3),
        is_active=True,
    )
    # One expired subscription (different calendar+provider to avoid unique constraint)
    CalendarWebhookSubscription.objects.create(
        calendar=calendar_b,
        organization=organization,
        provider=CalendarProvider.MICROSOFT,
        external_subscription_id="sub-2",
        channel_id="ch-2",
        callback_url="https://example.com/wh2",
        expires_at=now - datetime.timedelta(hours=1),
        is_active=True,
    )

    # Two recent events: one processed, one failed
    CalendarWebhookEvent.objects.create(
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        event_type="exists",
        external_calendar_id="wh_cal_001",
        external_event_id="",
        raw_payload={"raw": ""},
        processing_status=IncomingWebhookProcessingStatus.PROCESSED,
    )
    CalendarWebhookEvent.objects.create(
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        event_type="exists",
        external_calendar_id="wh_cal_001",
        external_event_id="",
        raw_payload={"raw": ""},
        processing_status=IncomingWebhookProcessingStatus.FAILED,
    )

    host = FakeHost()
    service = make_service(context, host)
    status: WebhookHealthStatus = service.get_webhook_health_status()

    assert status["total_subscriptions"] == 2
    assert status["active_subscriptions"] == 2
    assert status["expired_subscriptions"] == 1  # expires_at < now and is_active=True
    assert status["expiring_soon_subscriptions"] == 1  # expires_at in (now, now+24h)
    assert status["recent_events_count"] == 2
    assert status["failed_events_count"] == 1
    # (2 - 1) / 2 * 100 = 50%
    assert status["success_rate"] == 50.0


@pytest.mark.django_db
def test_get_webhook_health_status_requires_organization(
    user: User,
) -> None:
    """get_webhook_health_status raises ValueError when organization is not set."""
    context_no_org = CalendarServiceContext(
        organization=None,
        user_or_token=user,
        account=None,
        calendar_adapter=None,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )
    host = FakeHost()
    service = make_service(context_no_org, host)

    with pytest.raises(ValueError, match="Organization must be set"):
        service.get_webhook_health_status()
