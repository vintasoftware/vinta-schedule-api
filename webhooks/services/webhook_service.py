import datetime

import requests

from organizations.models import Organization
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.tasks import process_webhook_event


MAX_WEBHOOK_RETRIES = 5


class InitializedWebhook:
    organization: Organization


class WebhookService:
    def create_configuration(
        self, organization: Organization, event_type: WebhookEventType, url: str, headers: dict
    ) -> WebhookConfiguration:
        return WebhookConfiguration.objects.create(
            organization=organization, event_type=event_type, url=url, headers=headers
        )

    def update_configuration(
        self,
        configuration: WebhookConfiguration,
        event_type: WebhookEventType,
        url: str,
        headers: dict,
    ) -> WebhookConfiguration | None:
        configuration.url = url
        configuration.event_type = event_type
        configuration.headers = headers
        configuration.save()
        return configuration

    def delete_configuration(self, configuration: WebhookConfiguration) -> bool:
        configuration.deleted_at = datetime.datetime.now(tz=datetime.UTC)
        configuration.save()
        return True

    def send_event(
        self, organization: Organization, event_type: WebhookEventType, payload: dict
    ) -> list[WebhookEvent]:
        """
        Send webhook events to all configured URLs for the given event type.

        Args:
            event_type (WebhookEventType): The type of the event to send.
            payload (dict): The payload data to include in the event.
        """
        configurations = WebhookConfiguration.objects.filter(
            organization=organization, event_type=event_type, deleted_at__isnull=True
        )

        webhook_events = []
        for configuration in configurations:
            webhook_event = WebhookEvent(
                organization=organization,
                configuration=configuration,
                event_type=event_type,
                payload=payload,
            )
            webhook_event.save()
            webhook_events.append(webhook_event)
            self._schedule_webhook_event(event=webhook_event)
        return webhook_events

    def _schedule_webhook_event(self, event: WebhookEvent):
        process_webhook_event.delay(event_id=event.pk, organization_id=event.organization_id)

    def schedule_event_retry(
        self, event: WebhookEvent, use_current_configuration=False, is_manual=False
    ) -> WebhookEvent | None:
        """Schedule a retry for a failed webhook event.

        Args:
            event (WebhookEvent): The webhook event to retry.
            use_current_configuration (bool, optional): Whether to use the current configuration
                for the retry. This is useful for manual retries and debugging. Defaults to False.
            is_manual (bool, optional): Whether to skip the exponential backoff for the retry and
                skips the retry count limit. Defaults to False.

        Returns:
            WebhookEvent: The scheduled retry event.
        """
        retry_number = (event.retry_number or 0) + 1

        if not is_manual and retry_number > MAX_WEBHOOK_RETRIES:
            return None

        exponential_backoff = 0 if is_manual else 2 ** (retry_number - 1)
        retry_event = WebhookEvent.objects.create(
            organization=event.organization,
            configuration=event.configuration,
            event_type=event.event_type,
            url=(event.url if not use_current_configuration else event.configuration.url),
            headers=(
                event.headers if not use_current_configuration else event.configuration.headers
            ),
            payload=event.payload,
            status=WebhookStatus.PENDING,
            send_after=(
                datetime.datetime.now(tz=datetime.UTC)
                + datetime.timedelta(seconds=exponential_backoff)
            ),
            main_event=event.main_event if event.main_event else event,
            retry_number=retry_number,
        )

        process_webhook_event.apply_async(
            kwargs={
                "event_id": retry_event.pk,
                "organization_id": retry_event.organization_id,
            },
            countdown=exponential_backoff,
        )
        return retry_event

    def process_webhook_event(self, event: WebhookEvent) -> WebhookEvent:
        """
        Process a webhook event by sending an HTTP POST request to the configured URL.

        Args:
            event (WebhookEvent): The webhook event to process.

        Return:
            WebhookEvent: The processed webhook event.
        """

        try:
            response = requests.post(
                event.url,
                headers=event.headers,
                json=event.payload,
                timeout=60,
            )
        except requests.RequestException as e:
            event.status = WebhookStatus.FAILED
            event.response_body = {"error": str(e)}
            event.save()
            return event

        event.status = (
            WebhookStatus.SUCCESS
            if response.status_code >= 200 and response.status_code < 300
            else WebhookStatus.FAILED
        )
        event.response_status = response.status_code
        try:
            event.response_body = {"body": response.json()}
        except ValueError:
            event.response_body = {"body": response.text}
        event.response_headers = dict(response.headers)
        event.save()

        if event.status == WebhookStatus.FAILED and (event.retry_number or 0) < MAX_WEBHOOK_RETRIES:
            self.schedule_event_retry(event=event)

        return event
