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
from unittest.mock import MagicMock, patch

from django.test import override_settings
from django.urls import reverse

import pytest
from allauth.socialaccount.models import SocialAccount
from model_bakery import baker

from calendar_integration.constants import (
    CalendarProvider,
    CalendarSyncTriggerSource,
    CalendarType,
    IncomingWebhookProcessingStatus,
)
from calendar_integration.exceptions import (
    CalendarServiceOrganizationNotSetError,
    ServiceNotAuthenticatedError,
    WebhookIgnoredError,
    WebhookProcessingFailedError,
)
from calendar_integration.models import (
    Calendar,
    CalendarSync,
    CalendarWebhookEvent,
    CalendarWebhookSubscription,
    GoogleCalendarServiceAccount,
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
        self.request_webhook_triggered_sync_kwargs: list[dict[str, Any]] = []
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
        calendar: Calendar | None = None,
    ) -> CalendarSync | None:
        self.request_webhook_triggered_sync_calls.append((external_calendar_id, webhook_event))
        self.request_webhook_triggered_sync_kwargs.append({"calendar": calendar})
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


@pytest.fixture(autouse=True)
def https_api_domain(settings: Any) -> None:
    """Google only accepts HTTPS callbacks, and no channel is opened without one."""
    settings.API_DOMAIN = "https://api.example.com"


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


ROOM_EMAIL = "c_1882room@resource.calendar.google.com"


@pytest.fixture
def google_room(db: Any, organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Board Room",
        external_id="c_1882room",
        email=ROOM_EMAIL,
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
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
    persisted = CalendarWebhookSubscription.objects.filter_by_organization(
        calendar.organization_id
    ).get(
        id=sub.id,
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


@pytest.mark.django_db
def test_create_calendar_webhook_subscription_google_room_watches_resource_email(
    context: CalendarServiceContext,
    google_room: Calendar,
    fake_adapter: MagicMock,
) -> None:
    """Google's Calendar API knows a room by its resourceEmail, so the watch channel
    must be opened on the email, not on the Directory resourceId kept in external_id."""
    fake_adapter.create_webhook_subscription_with_tracking.return_value = {
        "channel_id": "channel-room",
        "resource_id": "resource-room",
        "resource_uri": f"https://www.googleapis.com/calendar/v3/calendars/{ROOM_EMAIL}/events",
        "expiration": "1700000000000",
        "calendar_id": ROOM_EMAIL,
        "callback_url": "https://example.com/webhook",
    }
    service = make_service(context, FakeHost(fake_adapter=fake_adapter))

    service.create_calendar_webhook_subscription(
        calendar=google_room,
        callback_url="https://example.com/webhook",
        expiration_hours=24,
    )

    call = fake_adapter.create_webhook_subscription_with_tracking.call_args
    assert (call.kwargs["resource_id"], call.kwargs["callback_url"]) == (
        ROOM_EMAIL,
        "https://example.com/webhook",
    )


# ---------------------------------------------------------------------------
# Tests: subscription refresh
# ---------------------------------------------------------------------------


def _watch_response(channel_id: str, resource_id: str, expiration_ms: int) -> dict[str, Any]:
    """What ``create_webhook_subscription_with_tracking`` returns for a Google channel."""
    return {
        "channel_id": channel_id,
        "resource_id": resource_id,
        "resource_uri": "https://www.googleapis.com/calendar/v3/calendars/wh_cal_001/events",
        "expiration": str(expiration_ms),
        "calendar_id": "wh_cal_001",
        "callback_url": "https://example.com/webhook",
    }


def _token_sent_to_google(fake_adapter: MagicMock) -> str:
    return fake_adapter.create_webhook_subscription_with_tracking.call_args.kwargs[
        "tracking_params"
    ]["token"]


@pytest.fixture
def social_context(
    organization: Organization, social_account: SocialAccount, fake_adapter: MagicMock
) -> CalendarServiceContext:
    """Authenticated the way ``authenticate()`` leaves it: the account is a SocialAccount."""
    return CalendarServiceContext(
        organization=organization,
        user_or_token=social_account.user,
        account=social_account,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )


@pytest.mark.django_db
@override_settings(API_DOMAIN="https://api.example.com")
def test_create_google_subscription_records_account_token_digest_and_callback(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    social_account: SocialAccount,
    fake_adapter: MagicMock,
) -> None:
    fake_adapter.create_webhook_subscription_with_tracking.return_value = _watch_response(
        "channel-new", "resource-new", 1_700_000_000_000
    )

    sub = make_service(social_context, FakeHost()).create_calendar_webhook_subscription(
        calendar=calendar, expiration_hours=48
    )

    call = fake_adapter.create_webhook_subscription_with_tracking.call_args
    token = _token_sent_to_google(fake_adapter)
    assert call.kwargs["callback_url"] == "https://api.example.com" + reverse(
        "calendar_integration:google_webhook", kwargs={"organization_id": calendar.organization_id}
    )
    assert call.kwargs["tracking_params"]["ttl_seconds"] == 48 * 3600
    assert len(token) >= 32
    assert (sub.social_account, sub.google_service_account, sub.account) == (
        social_account,
        None,
        social_account,
    )
    assert sub.verification_token == CalendarWebhookSubscription.hash_verification_token(token)
    assert sub.verification_token != token
    assert sub.matches_verification_token(token) is True
    assert sub.matches_verification_token("forged") is False


@pytest.mark.django_db
@override_settings(API_DOMAIN="localhost:3000", DEFAULT_PROTOCOL="http")
def test_callback_url_adds_the_scheme_when_api_domain_is_a_bare_host(
    calendar: Calendar,
) -> None:
    assert CalendarWebhookService._build_callback_url(
        calendar
    ) == "http://localhost:3000" + reverse(
        "calendar_integration:google_webhook", kwargs={"organization_id": calendar.organization_id}
    )


@pytest.mark.django_db
def test_refresh_webhook_subscription_google_replaces_the_channel(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    organization: Organization,
    social_account: SocialAccount,
    fake_adapter: MagicMock,
) -> None:
    """Google channels cannot be extended: renewing opens a new channel on the same row
    and only then stops the old one."""
    sub = CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="ch-old",
        external_resource_id="res-old",
        channel_id="ch-old",
        callback_url="https://example.com/wh",
        verification_token=CalendarWebhookSubscription.hash_verification_token("old-token"),
        expires_at=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=2),
    )
    fake_adapter.create_webhook_subscription_with_tracking.return_value = _watch_response(
        "ch-new", "res-new", 1_800_000_000_000
    )

    result = make_service(social_context, FakeHost()).refresh_webhook_subscription(
        subscription_id=sub.id
    )

    assert result is not None
    sub.refresh_from_db()
    token = _token_sent_to_google(fake_adapter)
    assert (
        sub.id,
        sub.channel_id,
        sub.external_subscription_id,
        sub.external_resource_id,
        sub.expires_at,
        sub.social_account,
        sub.is_active,
    ) == (
        result.id,
        "ch-new",
        "ch-new",
        "res-new",
        datetime.datetime.fromtimestamp(1_800_000_000, tz=datetime.UTC),
        social_account,
        True,
    )
    assert sub.matches_verification_token(token) is True
    assert sub.matches_verification_token("old-token") is False
    fake_adapter.stop_webhook_subscription.assert_called_once_with("ch-old", "res-old")
    call_names = [c[0] for c in fake_adapter.mock_calls]
    assert call_names.index("create_webhook_subscription_with_tracking") < call_names.index(
        "stop_webhook_subscription"
    )


@pytest.mark.django_db
def test_refresh_google_keeps_the_new_channel_when_stopping_the_old_one_fails(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    organization: Organization,
    fake_adapter: MagicMock,
) -> None:
    sub = CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="ch-old",
        external_resource_id="res-old",
        channel_id="ch-old",
        callback_url="https://example.com/wh",
    )
    fake_adapter.create_webhook_subscription_with_tracking.return_value = _watch_response(
        "ch-new", "res-new", 1_800_000_000_000
    )
    fake_adapter.stop_webhook_subscription.side_effect = ValueError("channel already gone")

    make_service(social_context, FakeHost()).refresh_webhook_subscription(subscription_id=sub.id)

    sub.refresh_from_db()
    assert (sub.channel_id, sub.is_active) == ("ch-new", True)


@pytest.mark.django_db
def test_refresh_google_requires_an_authenticated_service(
    unauthenticated_context: CalendarServiceContext,
    calendar: Calendar,
    organization: Organization,
) -> None:
    sub = CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="ch-old",
        channel_id="ch-old",
        callback_url="https://example.com/wh",
    )

    with pytest.raises(ServiceNotAuthenticatedError):
        make_service(unauthenticated_context, FakeHost()).refresh_webhook_subscription(
            subscription_id=sub.id
        )


# ---------------------------------------------------------------------------
# Tests: ensure_calendar_watch_channel
# ---------------------------------------------------------------------------


def _google_subscription(
    calendar: Calendar,
    social_account: SocialAccount | None,
    expires_in: datetime.timedelta | None,
    is_active: bool = True,
) -> CalendarWebhookSubscription:
    return CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=calendar.organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="ch-old",
        external_resource_id="res-old",
        channel_id="ch-old",
        callback_url="https://example.com/wh",
        social_account=social_account,
        is_active=is_active,
        expires_at=(
            datetime.datetime.now(tz=datetime.UTC) + expires_in if expires_in is not None else None
        ),
    )


@pytest.mark.django_db
def test_ensure_watch_channel_opens_one_for_a_calendar_without_a_channel(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    social_account: SocialAccount,
    fake_adapter: MagicMock,
) -> None:
    fake_adapter.create_webhook_subscription_with_tracking.return_value = _watch_response(
        "ch-new", "res-new", 1_800_000_000_000
    )

    sub = make_service(social_context, FakeHost()).ensure_calendar_watch_channel(calendar)

    assert sub is not None
    assert (sub.channel_id, sub.social_account) == ("ch-new", social_account)
    call = fake_adapter.create_webhook_subscription_with_tracking.call_args
    assert (call.kwargs["resource_id"], call.kwargs["tracking_params"]["ttl_seconds"]) == (
        "wh_cal_001",
        7 * 24 * 3600,
    )


@pytest.mark.django_db
def test_ensure_watch_channel_leaves_a_fresh_channel_alone(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    social_account: SocialAccount,
    fake_adapter: MagicMock,
) -> None:
    existing = _google_subscription(calendar, social_account, datetime.timedelta(days=3))

    sub = make_service(social_context, FakeHost()).ensure_calendar_watch_channel(calendar)

    assert sub == existing
    assert fake_adapter.mock_calls == []


@pytest.mark.parametrize(
    ("has_account", "expires_in", "is_active", "stops_old_channel"),
    [
        pytest.param(True, datetime.timedelta(hours=3), True, True, id="expiring-soon"),
        pytest.param(True, None, True, True, id="no-expiry-recorded"),
        pytest.param(False, datetime.timedelta(days=3), True, True, id="no-account-recorded"),
        # Deactivated rows are either already stopped or have no account left to stop
        # them with; their notifications are rejected either way.
        pytest.param(True, datetime.timedelta(days=3), False, False, id="deactivated"),
    ],
)
@pytest.mark.django_db
def test_ensure_watch_channel_replaces_a_channel_that_needs_it(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    social_account: SocialAccount,
    fake_adapter: MagicMock,
    has_account: bool,
    expires_in: datetime.timedelta | None,
    is_active: bool,
    stops_old_channel: bool,
) -> None:
    existing = _google_subscription(
        calendar, social_account if has_account else None, expires_in, is_active
    )
    fake_adapter.create_webhook_subscription_with_tracking.return_value = _watch_response(
        "ch-new", "res-new", 1_800_000_000_000
    )

    sub = make_service(social_context, FakeHost()).ensure_calendar_watch_channel(calendar)

    assert sub is not None
    assert (sub.id, sub.channel_id, sub.social_account, sub.is_active) == (
        existing.id,
        "ch-new",
        social_account,
        True,
    )
    assert fake_adapter.stop_webhook_subscription.call_args_list == (
        [(("ch-old", "res-old"),)] if stops_old_channel else []
    )
    assert list(
        CalendarWebhookSubscription.objects.filter_by_organization(calendar.organization_id)
        .filter(calendar=calendar)
        .values_list("id", flat=True)
    ) == [existing.id]


@pytest.mark.django_db
def test_ensure_watch_channel_closes_the_channel_of_a_calendar_that_stopped_syncing(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    social_account: SocialAccount,
    fake_adapter: MagicMock,
) -> None:
    existing = _google_subscription(calendar, social_account, datetime.timedelta(hours=3))
    calendar.sync_enabled = False
    calendar.save(update_fields=["sync_enabled"])

    assert make_service(social_context, FakeHost()).ensure_calendar_watch_channel(calendar) is None

    existing.refresh_from_db()
    assert existing.is_active is False
    fake_adapter.stop_webhook_subscription.assert_called_once_with("ch-old", "res-old")
    fake_adapter.create_webhook_subscription_with_tracking.assert_not_called()


@pytest.mark.django_db
def test_ensure_watch_channel_opens_nothing_without_an_https_callback(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    fake_adapter: MagicMock,
    settings: Any,
) -> None:
    """Google refuses a plain-HTTP callback, so local development opens no channel."""
    settings.API_DOMAIN = "localhost:3000"
    settings.DEFAULT_PROTOCOL = "http"

    assert make_service(social_context, FakeHost()).ensure_calendar_watch_channel(calendar) is None
    assert fake_adapter.mock_calls == []


@pytest.mark.django_db
def test_ensure_watch_channel_leaves_a_calendar_another_worker_is_handling(
    social_context: CalendarServiceContext,
    calendar: Calendar,
    fake_adapter: MagicMock,
) -> None:
    """Two concurrent openers would each open a channel at Google and leak one."""
    with patch(
        "calendar_integration.services.calendar_webhook_service.try_advisory_xact_lock",
        return_value=False,
    ) as lock:
        result = make_service(social_context, FakeHost()).ensure_calendar_watch_channel(calendar)

    assert result is None
    lock.assert_called_once_with(f"google-watch-channel:{calendar.id}")
    assert fake_adapter.mock_calls == []


@pytest.mark.django_db
def test_renewing_as_another_kind_of_account_replaces_the_recorded_account(
    calendar: Calendar,
    organization: Organization,
    social_account: SocialAccount,
    fake_adapter: MagicMock,
) -> None:
    """A channel opened by a service account and renewed by a social account records
    only the social account, and stays on the same row of the same organization."""
    service_account = GoogleCalendarServiceAccount.objects.create(
        organization=organization,
        email="sa@project.iam.gserviceaccount.com",
        private_key_id="key-id",
        private_key="key",
    )
    sub = _google_subscription(calendar, None, datetime.timedelta(hours=3))
    sub.google_service_account = service_account
    sub.save()
    fake_adapter.create_webhook_subscription_with_tracking.return_value = _watch_response(
        "ch-new", "res-new", 1_800_000_000_000
    )
    social_context = CalendarServiceContext(
        organization=organization,
        user_or_token=social_account.user,
        account=social_account,
        calendar_adapter=fake_adapter,
        calendar_permission_service=None,
        calendar_side_effects_service=None,
    )

    make_service(social_context, FakeHost()).refresh_webhook_subscription(subscription_id=sub.id)

    rows = CalendarWebhookSubscription.objects.filter_by_organization(organization).filter(
        calendar=calendar
    )
    assert [
        (r.id, r.organization_id, r.social_account, r.google_service_account) for r in rows
    ] == [(sub.id, organization.id, social_account, None)]


@pytest.mark.django_db
def test_ensure_watch_channel_skips_calendars_that_do_not_get_one(
    social_context: CalendarServiceContext,
    organization: Organization,
    fake_adapter: MagicMock,
) -> None:
    service = make_service(social_context, FakeHost())
    microsoft = Calendar.objects.create(
        name="MS",
        external_id="ms-1",
        provider=CalendarProvider.MICROSOFT,
        organization=organization,
    )
    sync_disabled = Calendar.objects.create(
        name="Off",
        external_id="off-1",
        provider=CalendarProvider.GOOGLE,
        sync_enabled=False,
        organization=organization,
    )
    no_provider_id = Calendar.objects.create(
        name="No id", external_id="", provider=CalendarProvider.GOOGLE, organization=organization
    )

    assert [
        service.ensure_calendar_watch_channel(c) for c in (microsoft, sync_disabled, no_provider_id)
    ] == [None, None, None]
    assert fake_adapter.mock_calls == []


# ---------------------------------------------------------------------------
# Tests: verify_google_channel
# ---------------------------------------------------------------------------


def _channel_headers(
    channel_id: str = "ch-1", token: str = "secret-token", state: str = "exists"
) -> dict[str, str]:
    return {
        "X-Goog-Channel-ID": channel_id,
        "X-Goog-Channel-Token": token,
        "X-Goog-Resource-State": state,
        "X-Goog-Resource-ID": "res-1",
        "X-Goog-Resource-URI": "https://www.googleapis.com/calendar/v3/calendars/wh_cal_001/events",
    }


@pytest.fixture
def verified_subscription(
    calendar: Calendar, organization: Organization, social_account: SocialAccount
) -> CalendarWebhookSubscription:
    return CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
        external_subscription_id="ch-1",
        external_resource_id="res-1",
        channel_id="ch-1",
        callback_url="https://example.com/wh",
        verification_token=CalendarWebhookSubscription.hash_verification_token("secret-token"),
        social_account=social_account,
    )


@pytest.mark.django_db
def test_verify_google_channel_returns_the_matching_subscription(
    unauthenticated_context: CalendarServiceContext,
    verified_subscription: CalendarWebhookSubscription,
) -> None:
    service = make_service(unauthenticated_context, FakeHost())

    assert service.verify_google_channel(_channel_headers()) == verified_subscription


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(_channel_headers(token="forged"), id="wrong-token"),
        pytest.param(_channel_headers(token=""), id="missing-token"),
        pytest.param(_channel_headers(channel_id="ch-unknown"), id="unknown-channel"),
        pytest.param(_channel_headers(channel_id=""), id="missing-channel"),
    ],
)
@pytest.mark.django_db
def test_verify_google_channel_rejects_unverified_notifications(
    unauthenticated_context: CalendarServiceContext,
    verified_subscription: CalendarWebhookSubscription,
    headers: dict[str, str],
) -> None:
    service = make_service(unauthenticated_context, FakeHost())

    with pytest.raises(WebhookProcessingFailedError):
        service.verify_google_channel(headers)


@pytest.mark.django_db
def test_verify_google_channel_rejects_a_deactivated_channel(
    unauthenticated_context: CalendarServiceContext,
    verified_subscription: CalendarWebhookSubscription,
) -> None:
    verified_subscription.is_active = False
    verified_subscription.save(update_fields=["is_active"])

    with pytest.raises(WebhookProcessingFailedError):
        make_service(unauthenticated_context, FakeHost()).verify_google_channel(_channel_headers())


@pytest.mark.django_db
def test_verify_google_channel_ignores_the_sync_handshake(
    unauthenticated_context: CalendarServiceContext,
) -> None:
    """Google sends ``sync`` when a channel opens, possibly before its row commits."""
    with pytest.raises(WebhookIgnoredError):
        make_service(unauthenticated_context, FakeHost()).verify_google_channel(
            _channel_headers(channel_id="ch-not-saved-yet", state="sync")
        )


@pytest.mark.django_db
def test_verified_notification_without_a_usable_account_is_ignored_not_pending(
    unauthenticated_context: CalendarServiceContext,
    verified_subscription: CalendarWebhookSubscription,
    fake_adapter: MagicMock,
) -> None:
    """Nothing ever processes ``PENDING`` events, so a notification that cannot sync
    must not be left looking like it will."""
    fake_adapter.validate_webhook_notification_static.return_value = {
        "provider": "google",
        "calendar_id": "wh_cal_001",
        "event_type": "exists",
    }
    host = FakeHost(fake_adapter=fake_adapter)

    event = make_service(unauthenticated_context, host).process_webhook_notification(
        provider="google",
        calendar_external_id="wh_cal_001",
        headers=_channel_headers(),
        subscription=verified_subscription,
    )

    assert event is not None
    assert event.processing_status == IncomingWebhookProcessingStatus.IGNORED
    assert host.request_webhook_triggered_sync_calls == []


@pytest.mark.django_db
def test_process_webhook_notification_with_subscription_syncs_its_calendar(
    social_context: CalendarServiceContext,
    organization: Organization,
    verified_subscription: CalendarWebhookSubscription,
    fake_adapter: MagicMock,
) -> None:
    """A verified channel names its calendar, so the notification syncs that calendar
    without trusting the calendar id in the headers."""
    fake_adapter.validate_webhook_notification_static.return_value = {
        "provider": "google",
        "calendar_id": "wh_cal_001",
        "event_type": "exists",
    }
    host = FakeHost(fake_adapter=fake_adapter)
    host.webhook_triggered_sync_return = CalendarSync.objects.create(
        calendar=verified_subscription.calendar,
        organization=organization,
        start_datetime=datetime.datetime.now(tz=datetime.UTC),
        end_datetime=datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(hours=1),
        should_update_events=True,
    )

    event = make_service(social_context, host).process_webhook_notification(
        provider="google",
        calendar_external_id="wh_cal_001",
        headers=_channel_headers(),
        subscription=verified_subscription,
    )

    assert event is not None
    verified_subscription.refresh_from_db()
    assert event.subscription == verified_subscription
    assert verified_subscription.last_notification_at is not None
    assert host.request_webhook_triggered_sync_kwargs == [
        {"calendar": verified_subscription.calendar}
    ]


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
# Tests: request_webhook_triggered_sync
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_request_webhook_triggered_sync_finds_google_room_by_resource_email(
    context: CalendarServiceContext,
    organization: Organization,
    google_room: Calendar,
) -> None:
    """A room's watch channel is keyed by its resourceEmail, so the notification names
    the email; the lookup must resolve it to the room even though external_id holds
    the Directory resourceId."""
    webhook_event = baker.make(
        CalendarWebhookEvent,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
    )
    host = FakeHost()
    service = make_service(context, host)

    service.request_webhook_triggered_sync(
        external_calendar_id=ROOM_EMAIL,
        webhook_event=webhook_event,
    )

    assert [call["calendar"] for call in host.request_calendar_sync_calls] == [google_room]


@pytest.mark.django_db
def test_request_webhook_triggered_sync_prefers_google_room_over_personal_duplicate(
    context: CalendarServiceContext,
    organization: Organization,
) -> None:
    """A room the account also lists among its own calendars is imported a second time
    as a PERSONAL row keyed by the room email. A notification for the room email must
    still sync the room, even when that duplicate row is older."""
    Calendar.objects.create(
        name="Board Room (calendar list)",
        external_id=ROOM_EMAIL,
        email=ROOM_EMAIL,
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.PERSONAL,
        sync_enabled=False,
        organization=organization,
    )
    room = Calendar.objects.create(
        name="Board Room",
        external_id="c_1882room",
        email=ROOM_EMAIL,
        provider=CalendarProvider.GOOGLE,
        calendar_type=CalendarType.RESOURCE,
        organization=organization,
    )
    webhook_event = baker.make(
        CalendarWebhookEvent,
        organization=organization,
        provider=CalendarProvider.GOOGLE,
    )
    host = FakeHost()

    make_service(context, host).request_webhook_triggered_sync(
        external_calendar_id=ROOM_EMAIL,
        webhook_event=webhook_event,
    )

    assert [call["calendar"] for call in host.request_calendar_sync_calls] == [room]


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
