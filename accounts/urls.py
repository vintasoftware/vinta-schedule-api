from django.urls import path

from allauth.headless.constants import Client

from accounts.views import (
    ProviderCallbackAPIView,
    ProviderRedirectAPIView,
    ProviderSignupView,
    RefreshTokenView,
)


urlpatterns = [
    path("app/v1/refresh-token/", RefreshTokenView.as_view(), name="refresh_token"),
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
    path(
        "app/v1/auth/provider/signup",
        ProviderSignupView.as_api_view(client=Client.APP),
        name="provider_signup_cookieless",
    ),
]
