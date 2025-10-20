"""
URL patterns for webhook endpoints.
"""

from django.urls import path

from calendar_integration.webhook_views import (
    GoogleCalendarWebhookView,
    MicrosoftCalendarWebhookView,
)


app_name = "calendar_integration"

urlpatterns = [
    path(
        "webhooks/google-calendar/<int:organization_id>/",
        GoogleCalendarWebhookView.as_view(),
        name="google_webhook",
    ),
    path(
        "webhooks/microsoft-calendar/<int:organization_id>/",
        MicrosoftCalendarWebhookView.as_view(),
        name="microsoft_webhook",
    ),
]
