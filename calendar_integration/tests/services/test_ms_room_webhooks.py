"""Microsoft room event subscriptions: room lifecycle signals, renewal and the sweep."""

import datetime
from unittest.mock import patch

from django.urls import reverse

import pytest
from freezegun import freeze_time

from calendar_integration.constants import CalendarProvider, ResourceSyncStatus
from calendar_integration.exceptions import MicrosoftAppOnlyTokenError
from calendar_integration.models import (
    CalendarWebhookSubscription,
    ResourceCalendarProviderLink,
)
from calendar_integration.services.calendar_webhook_service import ROOM_SUBSCRIPTION_LIFETIME
from calendar_integration.signals import resource_room_archived, resource_room_synced
from calendar_integration.tasks import (
    renew_microsoft_room_subscriptions_task,
    sweep_microsoft_room_events_task,
)
from calendar_integration.tests.ms_room_graph import ROOM_EMAIL, TENANT_ID, make_room
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag


pytestmark = pytest.mark.django_db

REVOKED_TENANT_ID = "99999999-8888-7777-6666-555555555555"


def refuse_tokens_for(tenant_id, token_provider):
    """Make the token provider fail for one tenant, as when it revoked consent."""
    working = token_provider.get_token.return_value

    def get_token(requested_tenant_id, force_refresh=False):
        if requested_tenant_id == tenant_id:
            raise MicrosoftAppOnlyTokenError()
        return working

    token_provider.get_token.side_effect = get_token


NOW = datetime.datetime(2026, 10, 6, 15, 0, tzinfo=datetime.UTC)


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Contoso")


def send_synced(calendar, captured, *, provider=CalendarProvider.MICROSOFT, created=True):
    with captured(execute=True):
        resource_room_synced.send(
            sender=None, calendar_id=calendar.id, provider=provider, created=created
        )


def send_archived(calendar, captured):
    with captured(execute=True):
        resource_room_archived.send(
            sender=None, calendar_id=calendar.id, provider=CalendarProvider.MICROSOFT
        )


def subscriptions_of(organization):
    with organization_context(organization):
        return list(CalendarWebhookSubscription.objects.order_by("pk"))


@freeze_time(NOW)
class TestSubscribeOnSynced:
    @pytest.mark.parametrize("created", [True, False], ids=["created", "linked-by-resync"])
    def test_synced_microsoft_room_is_subscribed_with_a_secret_client_state(
        self, organization, ms_room_graph, django_capture_on_commit_callbacks, settings, created
    ):
        settings.API_DOMAIN = "https://api.example.com"
        calendar = make_room(organization)

        send_synced(calendar, django_capture_on_commit_callbacks, created=created)

        [(method, path, body)] = ms_room_graph.subscription_requests
        [subscription] = subscriptions_of(organization)
        assert (method, path) == ("POST", "/subscriptions")
        assert body == {
            "changeType": "created,updated,deleted",
            "notificationUrl": "https://api.example.com"
            + reverse(
                "calendar_integration:microsoft_room_webhook",
                kwargs={"organization_id": organization.id},
            ),
            "resource": f"/users/{ROOM_EMAIL}/events",
            "expirationDateTime": (NOW + ROOM_SUBSCRIPTION_LIFETIME).isoformat(),
            "clientState": subscription.verification_token,
        }
        assert len(subscription.verification_token) >= 40
        assert (
            subscription.calendar_fk_id,
            subscription.provider,
            subscription.external_subscription_id,
            subscription.resource_uri,
            subscription.expires_at,
            subscription.is_active,
        ) == (
            calendar.id,
            CalendarProvider.MICROSOFT,
            "sub-1",
            f"/users/{ROOM_EMAIL}/events",
            NOW + ROOM_SUBSCRIPTION_LIFETIME,
            True,
        )

    def test_synced_again_keeps_the_live_subscription(
        self, organization, ms_room_graph, django_capture_on_commit_callbacks
    ):
        calendar = make_room(organization)

        send_synced(calendar, django_capture_on_commit_callbacks)
        send_synced(calendar, django_capture_on_commit_callbacks, created=False)

        assert [m for m, _, _ in ms_room_graph.subscription_requests] == ["POST"]
        assert len(subscriptions_of(organization)) == 1

    def test_google_room_is_not_subscribed(
        self, organization, ms_room_graph, django_capture_on_commit_callbacks
    ):
        calendar = make_room(organization)

        send_synced(calendar, django_capture_on_commit_callbacks, provider=CalendarProvider.GOOGLE)

        assert ms_room_graph.subscription_requests == []

    @pytest.mark.parametrize(
        ("flag_on", "write_enabled"), [(False, True), (True, False)], ids=["flag-off", "no-write"]
    )
    def test_room_outside_a_flag_on_write_enabled_org_is_not_subscribed(
        self,
        organization,
        ms_room_graph,
        django_capture_on_commit_callbacks,
        flag_on,
        write_enabled,
    ):
        calendar = make_room(organization, flag_on=flag_on, write_enabled=write_enabled)

        send_synced(calendar, django_capture_on_commit_callbacks)

        assert ms_room_graph.subscription_requests == []
        assert subscriptions_of(organization) == []


@freeze_time(NOW)
class TestUnsubscribeOnArchived:
    def test_archived_room_subscription_is_deleted_on_graph_and_deactivated(
        self, organization, ms_room_graph, django_capture_on_commit_callbacks
    ):
        calendar = make_room(organization)
        send_synced(calendar, django_capture_on_commit_callbacks)

        send_archived(calendar, django_capture_on_commit_callbacks)

        assert ms_room_graph.subscription_requests[-1] == ("DELETE", "/subscriptions/sub-1", None)
        assert ms_room_graph.subscriptions == {}
        [subscription] = subscriptions_of(organization)
        assert subscription.is_active is False

    def test_subscription_graph_already_dropped_is_still_deactivated(
        self, organization, ms_room_graph, django_capture_on_commit_callbacks
    ):
        calendar = make_room(organization)
        send_synced(calendar, django_capture_on_commit_callbacks)
        ms_room_graph.subscriptions.clear()

        send_archived(calendar, django_capture_on_commit_callbacks)

        [subscription] = subscriptions_of(organization)
        assert subscription.is_active is False


class TestRenewal:
    def subscribe(self, organization, captured, **room):
        with freeze_time(NOW - datetime.timedelta(hours=60)):
            calendar = make_room(organization, **room)
            send_synced(calendar, captured)
        return calendar

    def test_renews_only_subscriptions_expiring_within_a_day_in_flag_on_orgs(
        self, ms_room_graph, django_capture_on_commit_callbacks
    ):
        # Created 60h ago with a 70h lifetime: expires in 10h, so it is due.
        due = Organization.objects.create(name="Due")
        self.subscribe(due, django_capture_on_commit_callbacks)
        # Created now: expires in 70h, not due.
        fresh = Organization.objects.create(name="Fresh")
        with freeze_time(NOW):
            send_synced(make_room(fresh), django_capture_on_commit_callbacks)
        # Due, but the organization turned the flag off afterwards.
        flag_off = Organization.objects.create(name="Flag off")
        self.subscribe(flag_off, django_capture_on_commit_callbacks)
        OrganizationFeatureFlag.objects.filter_by_organization(flag_off.id).update(enabled=False)
        ms_room_graph.subscription_requests.clear()

        with freeze_time(NOW):
            renew_microsoft_room_subscriptions_task()

        assert ms_room_graph.subscription_requests == [
            (
                "PATCH",
                "/subscriptions/sub-1",
                {"expirationDateTime": (NOW + ROOM_SUBSCRIPTION_LIFETIME).isoformat()},
            )
        ]
        [renewed] = subscriptions_of(due)
        assert renewed.expires_at == NOW + ROOM_SUBSCRIPTION_LIFETIME

    def test_delegated_microsoft_subscription_is_left_alone(
        self, organization, ms_room_graph, django_capture_on_commit_callbacks
    ):
        calendar = self.subscribe(organization, django_capture_on_commit_callbacks)
        with organization_context(organization):
            CalendarWebhookSubscription.objects.filter(calendar=calendar).update(resource_uri="")
        ms_room_graph.subscription_requests.clear()

        with freeze_time(NOW):
            renew_microsoft_room_subscriptions_task()

        assert ms_room_graph.subscription_requests == []

    def test_subscription_graph_no_longer_has_is_created_again(
        self, organization, ms_room_graph, django_capture_on_commit_callbacks
    ):
        self.subscribe(organization, django_capture_on_commit_callbacks)
        old_client_state = subscriptions_of(organization)[0].verification_token
        ms_room_graph.subscriptions.clear()

        with freeze_time(NOW):
            renew_microsoft_room_subscriptions_task()

        [subscription] = subscriptions_of(organization)
        assert (subscription.external_subscription_id, subscription.is_active) == ("sub-2", True)
        assert subscription.verification_token != old_client_state
        assert subscription.expires_at == NOW + ROOM_SUBSCRIPTION_LIFETIME

    def test_an_organization_whose_tokens_fail_does_not_stop_the_others(
        self, ms_room_graph, ms_room_token_provider, django_capture_on_commit_callbacks
    ):
        revoked = Organization.objects.create(name="Revoked")
        self.subscribe(revoked, django_capture_on_commit_callbacks, tenant_id=REVOKED_TENANT_ID)
        working = Organization.objects.create(name="Working")
        self.subscribe(working, django_capture_on_commit_callbacks)
        refuse_tokens_for(REVOKED_TENANT_ID, ms_room_token_provider)
        ms_room_graph.subscription_requests.clear()

        with freeze_time(NOW):
            renew_microsoft_room_subscriptions_task()

        assert [(m, p) for m, p, _ in ms_room_graph.subscription_requests] == [
            ("PATCH", "/subscriptions/sub-2")
        ]
        [renewed] = subscriptions_of(working)
        assert renewed.expires_at == NOW + ROOM_SUBSCRIPTION_LIFETIME

    def test_a_failed_re_create_does_not_stop_the_others(
        self, ms_room_graph, django_capture_on_commit_callbacks
    ):
        dropped = Organization.objects.create(name="Dropped")
        self.subscribe(dropped, django_capture_on_commit_callbacks)
        working = Organization.objects.create(name="Working")
        self.subscribe(working, django_capture_on_commit_callbacks)
        # Graph lost the first subscription, and refuses to create it again.
        del ms_room_graph.subscriptions["sub-1"]
        ms_room_graph.refuse_subscription_creates = True

        with freeze_time(NOW):
            renew_microsoft_room_subscriptions_task()

        [renewed] = subscriptions_of(working)
        assert renewed.expires_at == NOW + ROOM_SUBSCRIPTION_LIFETIME


@freeze_time(NOW)
class TestSweep:
    def test_syncs_and_resubscribes_every_synced_microsoft_room(self, organization, ms_room_graph):
        calendar = make_room(organization)
        with organization_context(organization):
            ResourceCalendarProviderLink.objects.create(
                organization=organization,
                calendar=calendar,
                provider=CalendarProvider.MICROSOFT,
                sync_status=ResourceSyncStatus.SYNCED,
            )

        with patch(
            "calendar_integration.tasks.room_event_sync_tasks.sync_microsoft_room_events_task.delay"
        ) as delay:
            sweep_microsoft_room_events_task()

        delay.assert_called_once_with(calendar.id, organization.id)
        [subscription] = subscriptions_of(organization)
        assert subscription.is_active is True

    def test_an_organization_whose_tokens_fail_does_not_stop_the_others(
        self, ms_room_graph, ms_room_token_provider
    ):
        rooms = []
        for name, tenant_id in [("Revoked", REVOKED_TENANT_ID), ("Working", TENANT_ID)]:
            organization = Organization.objects.create(name=name)
            calendar = make_room(organization, tenant_id=tenant_id)
            with organization_context(organization):
                ResourceCalendarProviderLink.objects.create(
                    organization=organization,
                    calendar=calendar,
                    provider=CalendarProvider.MICROSOFT,
                    sync_status=ResourceSyncStatus.SYNCED,
                )
            rooms.append((organization, calendar))
        refuse_tokens_for(REVOKED_TENANT_ID, ms_room_token_provider)

        with patch(
            "calendar_integration.tasks.room_event_sync_tasks.sync_microsoft_room_events_task.delay"
        ) as delay:
            sweep_microsoft_room_events_task()

        assert [call.args for call in delay.call_args_list] == [
            (calendar.id, organization.id) for organization, calendar in rooms
        ]
        assert subscriptions_of(rooms[0][0]) == []
        [subscription] = subscriptions_of(rooms[1][0])
        assert subscription.is_active is True
