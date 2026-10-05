"""Opening and renewing Google push channels from Celery.

A channel is opened (or renewed) after every successful sync, as the account the
sync ran as, and an hourly sweep renews channels close to expiring as the account
recorded on each one.
"""

import datetime
from unittest.mock import MagicMock, patch

from django.utils import timezone

import pytest
from allauth.socialaccount.models import SocialAccount

from calendar_integration.constants import CalendarProvider, CalendarSyncStatus
from calendar_integration.exceptions import InvalidCalendarTokenError
from calendar_integration.models import (
    Calendar,
    CalendarSync,
    CalendarWebhookSubscription,
    GoogleCalendarServiceAccount,
)
from calendar_integration.tasks.calendar_sync_tasks import sync_calendar_task
from calendar_integration.tasks.webhook_channel_tasks import (
    renew_google_calendar_watch_channel_task,
    renew_google_calendar_watch_channels_task,
)
from organizations.models import Organization
from users.models import User


@pytest.fixture
def organization(db) -> Organization:
    return Organization.objects.create(name="Channel Org")


@pytest.fixture
def social_account(db) -> SocialAccount:
    user = User.objects.create_user(email="channels@example.com", password="pass")  # noqa: S106
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="ch-uid")


@pytest.fixture
def calendar(organization: Organization) -> Calendar:
    return Calendar.objects.create(
        name="Channel Calendar",
        external_id="channels@example.com",
        provider=CalendarProvider.GOOGLE,
        organization=organization,
    )


@pytest.fixture
def calendar_sync(calendar: Calendar, organization: Organization) -> CalendarSync:
    return CalendarSync.objects.create(
        calendar=calendar,
        organization=organization,
        start_datetime=datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC),
        end_datetime=datetime.datetime(2026, 10, 2, tzinfo=datetime.UTC),
        should_update_events=True,
    )


def _subscription(
    calendar: Calendar,
    expires_in: datetime.timedelta | None,
    social_account: SocialAccount | None = None,
    provider: str = CalendarProvider.GOOGLE,
    is_active: bool = True,
) -> CalendarWebhookSubscription:
    return CalendarWebhookSubscription.objects.create(
        calendar=calendar,
        organization=calendar.organization,
        provider=provider,
        external_subscription_id=f"ch-{calendar.id}-{provider}",
        channel_id=f"ch-{calendar.id}-{provider}",
        callback_url="https://example.com/wh",
        social_account=social_account,
        is_active=is_active,
        expires_at=timezone.now() + expires_in if expires_in is not None else None,
    )


def _finish_sync_with(status: str):
    def sync_events(calendar_sync: CalendarSync) -> None:
        calendar_sync.status = status

    return sync_events


# ---------------------------------------------------------------------------
# After a sync
# ---------------------------------------------------------------------------


def test_successful_sync_opens_or_renews_the_calendar_channel(
    social_account, calendar, calendar_sync, organization
):
    service = MagicMock()
    service.sync_events.side_effect = _finish_sync_with(CalendarSyncStatus.SUCCESS)

    sync_calendar_task(
        "social_account",
        social_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=service,
    )

    service.ensure_calendar_watch_channel.assert_called_once_with(calendar)


def test_failed_sync_leaves_the_channel_alone(
    social_account, calendar, calendar_sync, organization
):
    service = MagicMock()
    service.sync_events.side_effect = _finish_sync_with(CalendarSyncStatus.FAILED)

    sync_calendar_task(
        "social_account",
        social_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=service,
    )

    service.ensure_calendar_watch_channel.assert_not_called()


def test_channel_error_does_not_fail_the_sync_task(
    social_account, calendar, calendar_sync, organization
):
    """The sync already succeeded; the next sync or the hourly sweep retries the channel."""
    service = MagicMock()
    service.sync_events.side_effect = _finish_sync_with(CalendarSyncStatus.SUCCESS)
    service.ensure_calendar_watch_channel.side_effect = ValueError("Google said no")

    sync_calendar_task(
        "social_account",
        social_account.id,
        calendar_sync.id,
        organization.id,
        calendar_service=service,
    )

    service.ensure_calendar_watch_channel.assert_called_once_with(calendar)


def test_programming_error_while_opening_the_channel_is_not_swallowed(
    social_account, calendar, calendar_sync, organization
):
    """Only provider and account errors are tolerated; a bug must fail loudly instead of
    quietly leaving calendars without push notifications."""
    service = MagicMock()
    service.sync_events.side_effect = _finish_sync_with(CalendarSyncStatus.SUCCESS)
    service.ensure_calendar_watch_channel.side_effect = AttributeError("bug")

    with pytest.raises(AttributeError):
        sync_calendar_task(
            "social_account",
            social_account.id,
            calendar_sync.id,
            organization.id,
            calendar_service=service,
        )


# ---------------------------------------------------------------------------
# Hourly sweep
# ---------------------------------------------------------------------------


def test_sweep_queues_only_active_google_channels_due_for_renewal(organization, social_account):
    calendars = [
        Calendar.objects.create(
            name=f"Cal {n}",
            external_id=f"cal-{n}@example.com",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        )
        for n in range(6)
    ]
    expiring = _subscription(calendars[0], datetime.timedelta(hours=3), social_account)
    expired = _subscription(calendars[1], -datetime.timedelta(hours=1), social_account)
    no_expiry = _subscription(calendars[2], None, social_account)
    _subscription(calendars[3], datetime.timedelta(days=4), social_account)  # fresh
    _subscription(calendars[4], datetime.timedelta(hours=3), social_account, is_active=False)
    _subscription(calendars[5], datetime.timedelta(hours=3), provider=CalendarProvider.MICROSOFT)

    with patch(
        "calendar_integration.tasks.webhook_channel_tasks."
        "renew_google_calendar_watch_channel_task.delay"
    ) as delay:
        renew_google_calendar_watch_channels_task()

    assert sorted(call.args for call in delay.call_args_list) == sorted(
        (sub.id, organization.id) for sub in (expiring, expired, no_expiry)
    )


def test_sweep_spans_organizations_and_hands_each_task_its_own(organization, social_account):
    """The scheduler has no organization bound; each renewal task binds its own."""
    other = Organization.objects.create(name="Other Channel Org")
    mine = _subscription(
        Calendar.objects.create(
            name="Mine",
            external_id="mine@example.com",
            provider=CalendarProvider.GOOGLE,
            organization=organization,
        ),
        datetime.timedelta(hours=1),
        social_account,
    )
    theirs = _subscription(
        Calendar.objects.create(
            name="Theirs",
            external_id="theirs@example.com",
            provider=CalendarProvider.GOOGLE,
            organization=other,
        ),
        datetime.timedelta(hours=1),
    )

    with patch(
        "calendar_integration.tasks.webhook_channel_tasks."
        "renew_google_calendar_watch_channel_task.delay"
    ) as delay:
        renew_google_calendar_watch_channels_task()

    assert sorted(call.args for call in delay.call_args_list) == sorted(
        [(mine.id, organization.id), (theirs.id, other.id)]
    )


def test_renewal_authenticates_as_the_recorded_account(organization, calendar, social_account):
    subscription = _subscription(calendar, datetime.timedelta(hours=3), social_account)
    service = MagicMock()

    renew_google_calendar_watch_channel_task(
        subscription.id, organization.id, calendar_service=service
    )

    service.authenticate.assert_called_once_with(account=social_account, organization=organization)
    service.ensure_calendar_watch_channel.assert_called_once_with(calendar)


def test_renewal_of_a_room_uses_the_service_account(organization, calendar):
    service_account = GoogleCalendarServiceAccount.objects.create(
        organization=organization,
        email="sa@project.iam.gserviceaccount.com",
        private_key_id="key-id",
        private_key="key",
    )
    subscription = _subscription(calendar, datetime.timedelta(hours=3))
    subscription.google_service_account = service_account
    subscription.save()
    service = MagicMock()

    renew_google_calendar_watch_channel_task(
        subscription.id, organization.id, calendar_service=service
    )

    service.authenticate.assert_called_once_with(account=service_account, organization=organization)


def test_renewal_without_a_recorded_account_deactivates_the_channel(organization, calendar):
    """There is no one to renew it as, so the sweep would retry it every hour forever.
    The calendar's next sync opens a fresh channel."""
    subscription = _subscription(calendar, datetime.timedelta(hours=3))
    service = MagicMock()

    renew_google_calendar_watch_channel_task(
        subscription.id, organization.id, calendar_service=service
    )

    service.authenticate.assert_not_called()
    service.ensure_calendar_watch_channel.assert_not_called()
    subscription.refresh_from_db()
    assert subscription.is_active is False


def test_renewal_with_a_revoked_token_deactivates_the_channel(
    organization, calendar, social_account
):
    subscription = _subscription(calendar, datetime.timedelta(hours=3), social_account)
    service = MagicMock()
    service.authenticate.side_effect = InvalidCalendarTokenError("reauthenticate")

    renew_google_calendar_watch_channel_task(
        subscription.id, organization.id, calendar_service=service
    )

    service.ensure_calendar_watch_channel.assert_not_called()
    subscription.refresh_from_db()
    assert subscription.is_active is False


def test_renewal_of_a_deactivated_channel_does_nothing(organization, calendar, social_account):
    subscription = _subscription(
        calendar, datetime.timedelta(hours=3), social_account, is_active=False
    )
    service = MagicMock()

    renew_google_calendar_watch_channel_task(
        subscription.id, organization.id, calendar_service=service
    )

    service.authenticate.assert_not_called()
