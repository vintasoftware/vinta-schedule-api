"""The Microsoft room notification endpoint: handshake, clientState check, enqueueing."""

import json
from unittest.mock import patch
from urllib.parse import urlencode

from django.test import Client
from django.urls import reverse

import pytest

from calendar_integration.constants import CalendarProvider
from calendar_integration.models import CalendarWebhookSubscription
from calendar_integration.signals import resource_room_synced
from calendar_integration.tests.ms_room_graph import make_room
from common.organization_context import organization_context
from organizations.models import Organization


pytestmark = pytest.mark.django_db

SYNC_TASK_DELAY = (
    "calendar_integration.tasks.room_event_sync_tasks.sync_microsoft_room_events_task.delay"
)


@pytest.fixture
def organization() -> Organization:
    return Organization.objects.create(name="Contoso")


@pytest.fixture
def subscription(organization, ms_room_graph, django_capture_on_commit_callbacks):
    """A room subscribed through the real signal, task and service."""
    calendar = make_room(organization)
    with django_capture_on_commit_callbacks(execute=True):
        resource_room_synced.send(
            sender=None, calendar_id=calendar.id, provider=CalendarProvider.MICROSOFT, created=True
        )
    with organization_context(organization):
        return CalendarWebhookSubscription.objects.get(calendar=calendar)


def url(organization_id: int) -> str:
    return reverse(
        "calendar_integration:microsoft_room_webhook", kwargs={"organization_id": organization_id}
    )


def notify(organization_id, captured, *notifications):
    with captured(execute=True), patch(SYNC_TASK_DELAY) as delay:
        response = Client().post(
            url(organization_id),
            data=json.dumps({"value": list(notifications)}),
            content_type="application/json",
        )
    return response, delay


def notification(subscription_id: str, client_state: str) -> dict:
    return {
        "subscriptionId": subscription_id,
        "clientState": client_state,
        "changeType": "updated",
        "resource": "Users/room-a@contoso.com/Events/AAMk",
    }


class TestHandshake:
    def test_validation_token_is_echoed_as_plain_text(self, organization):
        token = "Validation: Testing client application reachability for subscription Request-Id"

        response = Client().post(f"{url(organization.id)}?{urlencode({'validationToken': token})}")

        assert response.status_code == 200
        assert response["Content-Type"].startswith("text/plain")
        assert response.content.decode() == token

    def test_overlong_validation_token_is_refused(self, organization):
        response = Client().post(f"{url(organization.id)}?validationToken={'x' * 2000}")

        assert response.status_code == 400


class TestNotifications:
    def test_valid_notification_enqueues_one_delta_sync(
        self, organization, subscription, django_capture_on_commit_callbacks
    ):
        token = subscription.verification_token
        response, delay = notify(
            organization.id,
            django_capture_on_commit_callbacks,
            notification(subscription.external_subscription_id, token),
            notification(subscription.external_subscription_id, token),
        )

        assert response.status_code == 202
        delay.assert_called_once_with(subscription.calendar_fk_id, organization.id)
        with organization_context(organization):
            subscription.refresh_from_db()
        assert subscription.last_notification_at is not None

    def test_wrong_client_state_is_refused_and_enqueues_nothing(
        self, organization, subscription, django_capture_on_commit_callbacks
    ):
        response, delay = notify(
            organization.id,
            django_capture_on_commit_callbacks,
            notification(subscription.external_subscription_id, subscription.verification_token),
            notification(subscription.external_subscription_id, "guessed-secret"),
        )

        assert response.status_code == 403
        delay.assert_not_called()

    def test_missing_client_state_is_refused(
        self, organization, subscription, django_capture_on_commit_callbacks
    ):
        item = notification(subscription.external_subscription_id, "")
        del item["clientState"]

        response, delay = notify(organization.id, django_capture_on_commit_callbacks, item)

        assert response.status_code == 403
        delay.assert_not_called()

    def test_notification_sent_to_another_organization_is_ignored(
        self, organization, subscription, django_capture_on_commit_callbacks
    ):
        other = Organization.objects.create(name="Fabrikam")

        response, delay = notify(
            other.id,
            django_capture_on_commit_callbacks,
            notification(subscription.external_subscription_id, subscription.verification_token),
        )

        assert response.status_code == 202
        delay.assert_not_called()

    def test_inactive_subscription_is_ignored(
        self, organization, subscription, django_capture_on_commit_callbacks
    ):
        with organization_context(organization):
            CalendarWebhookSubscription.objects.filter(pk=subscription.pk).update(is_active=False)

        response, delay = notify(
            organization.id,
            django_capture_on_commit_callbacks,
            notification(subscription.external_subscription_id, subscription.verification_token),
        )

        assert response.status_code == 202
        delay.assert_not_called()

    @pytest.mark.parametrize(
        "body", [b"not json", b"[]", b'{"value": [{"clientState": "x"}]}'], ids=str
    )
    def test_body_that_is_not_a_graph_notification_is_refused(self, organization, body):
        with patch(SYNC_TASK_DELAY) as delay:
            response = Client().post(
                url(organization.id), data=body, content_type="application/json"
            )

        assert response.status_code == 400
        delay.assert_not_called()
