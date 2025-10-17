"""
URL patterns for webhook endpoints.
"""

from django.urls import path

from calendar_integration.webhook_views import (
    GoogleCalendarWebhookView,
    MicrosoftCalendarWebhookView,
)


app_name = "calendar_webhooks"

urlpatterns = [
    path(
        "webhooks/google-calendar/",
        GoogleCalendarWebhookView.as_view(),
        name="google-calendar-webhook",
    ),
    path(
        "webhooks/microsoft-calendar/",
        MicrosoftCalendarWebhookView.as_view(),
        name="microsoft-calendar-webhook",
    ),
]
