from django.db import models

from organizations.models import OrganizationForeignKey, OrganizationModel
from webhooks.constants import WebhookEventType, WebhookStatus


class WebhookConfiguration(OrganizationModel):
    event_type = models.CharField(
        max_length=255,
        choices=WebhookEventType,
        default=WebhookEventType.CALENDAR_EVENT_CREATED,
    )
    url = models.URLField(max_length=2000)
    headers = models.JSONField(default=dict)
    deleted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"WebhookConfiguration(id={self.id}, event_type={self.event_type}, url={self.url})"


class WebhookEvent(OrganizationModel):
    configuration = OrganizationForeignKey(WebhookConfiguration, on_delete=models.CASCADE)
    event_type = models.CharField(max_length=255, choices=WebhookEventType)
    url = models.URLField(max_length=2000)
    status = models.CharField(
        max_length=50,
        choices=WebhookStatus,
        default=WebhookStatus.PENDING,
    )
    headers = models.JSONField(default=dict)
    payload = models.JSONField()
    response_status = models.PositiveBigIntegerField(null=True, blank=True)
    response_body = models.JSONField(null=True, blank=True)
    response_headers = models.JSONField(null=True, blank=True)

    main_event = OrganizationForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        help_text="Reference to the main event in case of retries",
    )
    retry_number = models.PositiveIntegerField(null=True, blank=True, default=None)
    send_after = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"WebhookEvent(id={self.id}, event_type={self.event_type}, url={self.url}))"
