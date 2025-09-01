import django_virtual_models as v

from webhooks.models import WebhookConfiguration, WebhookEvent


class WebhookConfigurationVirtualModel(v.VirtualModel):
    class Meta:
        model = WebhookConfiguration


class WebhookEventVirtualModel(v.VirtualModel):
    configuration = WebhookConfigurationVirtualModel()

    class Meta:
        model = WebhookEvent
