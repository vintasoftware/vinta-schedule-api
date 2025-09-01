from common.types import RouteDict

from .views import WebhookConfigurationViewSet, WebhookEventViewSet


routes: list[RouteDict] = [
    {
        "regex": r"webhook-configurations",
        "viewset": WebhookConfigurationViewSet,
        "basename": "WebhookConfigurations",
    },
    {
        "regex": r"webhook-events",
        "viewset": WebhookEventViewSet,
        "basename": "WebhookEvents",
    },
]
