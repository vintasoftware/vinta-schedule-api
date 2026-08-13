from typing import ClassVar

from django.db import models

from vinta_orgs.mixins import SingleOrganizationModelMixin

from common.fields import OrganizationSafeForeignKey
from common.managers import OrganizationScopedManager
from common.models import BaseModel, SafeRelationNullInitMixin
from webhooks.constants import WebhookEventType, WebhookStatus
from webhooks.managers import WebhookConfigurationManager


class WebhookConfiguration(SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel):
    objects: ClassVar[WebhookConfigurationManager] = WebhookConfigurationManager()

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


class WebhookEvent(SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel):
    objects: ClassVar[OrganizationScopedManager] = OrganizationScopedManager()

    configuration = OrganizationSafeForeignKey(WebhookConfiguration, on_delete=models.CASCADE)
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

    main_event = OrganizationSafeForeignKey(
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
