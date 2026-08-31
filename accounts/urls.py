from django.urls import path

from allauth.headless.constants import Client

from accounts.views import (
    AtomicSignupView,
    ProviderCallbackAPIView,
    ProviderRedirectAPIView,
)


# Included at the same ``auth/`` prefix as, and **before**, ``allauth.headless.urls``
# (see vinta_schedule_api/urls.py) -- so a path listed here shadows allauth's own.
urlpatterns = [
    # Both clients in ``HEADLESS_CLIENTS``, on the same paths allauth registers for
    # them, pointed at the atomic subclass. See ``AtomicSignupView`` for what allauth's
    # own view leaves behind when the verification email cannot be sent -- it is the
    # signup flow that is at fault, so it is wrong for either client.
    path(
        "app/v1/auth/signup",
        AtomicSignupView.as_api_view(client=Client.APP),
        name="atomic_signup",
    ),
    path(
        "browser/v1/auth/signup",
        AtomicSignupView.as_api_view(client=Client.BROWSER),
        name="atomic_signup_browser",
    ),
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
