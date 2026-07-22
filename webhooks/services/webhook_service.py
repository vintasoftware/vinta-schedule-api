import datetime
from typing import TYPE_CHECKING, Annotated, Any, cast

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.db import transaction

import requests
from dependency_injector.wiring import Provide, inject

from organizations.models import Organization
from payments.billing_constants import LimitedResource
from payments.exceptions import OverLimitError
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.models import WebhookConfiguration, WebhookEvent
from webhooks.services.payloads import WebhookEnvelope
from webhooks.tasks import process_webhook_event


if TYPE_CHECKING:
    from payments.services.entitlement_service import EntitlementService


MAX_WEBHOOK_RETRIES = 5

_url_validator = URLValidator(schemes=["http", "https"])


class InitializedWebhook:
    organization: Organization


class WebhookService:
    @inject
    def __init__(
        self,
        entitlement_service: Annotated[
            "EntitlementService | None", Provide["entitlement_service"]
        ] = None,
    ) -> None:
        self.entitlement_service = entitlement_service

    def _validate_config_fields(self, event_type: str, url: str) -> None:
        """Validate event_type and url before persisting a WebhookConfiguration.

        Raises:
            ValueError: if event_type is not a known WebhookEventType value.
            ValueError: if url is empty or not a valid http(s) URL.
        """
        valid_event_types = {et.value for et in WebhookEventType}
        if event_type not in valid_event_types:
            raise ValueError(
                f"Invalid event_type '{event_type}'. "
                f"Valid values are: {', '.join(sorted(valid_event_types))}."
            )
        if not url:
            raise ValueError("url must not be empty.")
        try:
            _url_validator(url)
        except DjangoValidationError as e:
            raise ValueError(f"Invalid url '{url}'. Must be a valid http(s) URL.") from e

    @transaction.atomic()
    def create_configuration(
        self,
        organization: Organization,
        event_type: str,
        url: str,
        headers: dict,
        bypass_limits: bool = False,
    ) -> WebhookConfiguration:
        """Create a ``WebhookConfiguration``, guarded on the ``webhook_subscriptions``
        pre-paid limit.

        :param bypass_limits: When True, skips the ``webhook_subscriptions`` limit
            guard below. Only management commands and one-off repair scripts should
            pass this -- never a request-handling path.
        :raises OverLimitError: When the organization is at its effective
            ``webhook_subscriptions`` ceiling. Nothing is created. Checked and locked
            (``SELECT ... FOR UPDATE`` on the billing root's subscription) inside this
            method's own transaction, so two concurrent creates for the last unit of
            capacity serialize on that row.

            The counter this guards (``EntitlementService._count_webhook_subscriptions``)
            counts ``WebhookConfiguration.objects.live()`` -- rows with
            ``deleted_at__isnull=True``. A freshly created row always has
            ``deleted_at=None``, so it is unconditionally "live" and always feeds the
            same counter this check reads: there is no separate predicate to keep in
            sync.
        """
        self._validate_config_fields(event_type, url)
        if not bypass_limits and self.entitlement_service is not None:
            result = self.entitlement_service.check_limit(
                organization, LimitedResource.WEBHOOK_SUBSCRIPTIONS, lock=True
            )
            if not result.allowed:
                raise OverLimitError.from_check_result(result)
        return WebhookConfiguration.objects.create(
            organization=organization, event_type=event_type, url=url, headers=headers
        )

    def update_configuration(
        self,
        configuration: WebhookConfiguration,
        event_type: str,
        url: str,
        headers: dict,
        bypass_limits: bool = False,
    ) -> WebhookConfiguration:
        """Update ``configuration``.

        :param bypass_limits: When True, skips the restricted-organization check
            below. Only management commands and one-off repair scripts should
            pass this -- never a request-handling path.
        :raises OverLimitError: When ``configuration``'s organization is
            restricted.
        """
        if not bypass_limits and self.entitlement_service is not None:
            self.entitlement_service.check_not_restricted(configuration.organization)
        self._validate_config_fields(event_type, url)
        configuration.url = url
        configuration.event_type = event_type
        configuration.headers = headers
        configuration.save()
        return configuration

    def delete_configuration(
        self, configuration: WebhookConfiguration, bypass_limits: bool = False
    ) -> bool:
        """Soft-delete ``configuration``.

        :param bypass_limits: When True, skips the restricted-organization check
            below. Only management commands and one-off repair scripts should
            pass this -- never a request-handling path.
        :raises OverLimitError: When ``configuration``'s organization is
            restricted.
        """
        if not bypass_limits and self.entitlement_service is not None:
            self.entitlement_service.check_not_restricted(configuration.organization)
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

        envelope: WebhookEnvelope = {
            "id": str(event.main_event_fk_id or event.id),
            "type": event.event_type,
            "timestamp": event.created.isoformat(),
            "data": event.payload,
        }

        try:
            response = requests.post(
                event.url,
                headers=event.headers,
                json=cast(dict[str, Any], envelope),
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
