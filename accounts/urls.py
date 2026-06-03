from django.urls import path

from allauth.headless.constants import Client

from accounts.views import (
    ProviderCallbackAPIView,
    ProviderRedirectAPIView,
)


urlpatterns = [
    path(
        "app/v1/auth/provider/redirect-json/",
        ProviderRedirectAPIView.as_api_view(client=Client.APP),
        name="provider_redirect_json",
    ),
    path(
        "app/v1/auth/provider/callback-json/",
        ProviderCallbackAPIView.as_api_view(client=Client.APP),
        name="provider_callback_json",
    ),
]
