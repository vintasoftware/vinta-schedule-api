import json

from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from allauth.headless.base.response import (
    AuthenticationResponse,
    ConflictResponse,
    ForbiddenResponse,
)
from allauth.headless.base.views import APIView as AllauthAPIView
from allauth.headless.socialaccount.forms import RedirectToProviderForm
from allauth.headless.socialaccount.inputs import (
    SignupInput,
)
from allauth.headless.socialaccount.response import (
    SocialLoginResponse,
)
from allauth.socialaccount.adapter import get_adapter as get_socialaccount_adapter
from allauth.socialaccount.helpers import (
    complete_social_login,
)
from allauth.socialaccount.internal import flows, statekit
from allauth.socialaccount.models import SocialApp
from allauth.socialaccount.providers.base import ProviderException
from allauth.socialaccount.providers.base.constants import AuthError
from allauth.socialaccount.providers.oauth2.client import (
    OAuth2Error,
)
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider
from drf_spectacular.utils import extend_schema
from requests.exceptions import RequestException
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import RefreshTokenSerializer


class RefreshTokenView(APIView):
    """
    View to handle refresh token logic.
    """

    permission_classes = (AllowAny,)

    @extend_schema(exclude=True)
    def post(self, request, *args, **kwargs):
        """
        Handle the refresh token request.
        """
        serializer = RefreshTokenSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        # Call the save method to handle the refresh token logic
        access_and_refresh_tokens = serializer.save()

        return Response(access_and_refresh_tokens, status=status.HTTP_200_OK)


class ProviderRedirectAPIView(AllauthAPIView):
    """
    Custom endpoint to initiate provider redirect flow for non-browser clients.
    Returns the provider redirect URL and a session token in JSON.
    """

    handle_json_input = False

    @csrf_exempt
    def dispatch(self, request, *args, **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        data = json.loads(request.body.decode("utf-8"))
        form = RedirectToProviderForm(data)
        if not form.is_valid():
            return JsonResponse(
                form.errors,
                status=status.HTTP_400_BAD_REQUEST,
            )
        provider = form.cleaned_data["provider"]
        next_url = form.cleaned_data["callback_url"]
        process = form.cleaned_data["process"]

        # Generate the provider's authorization URL
        app = provider.app
        oauth2_adapter = provider.get_oauth2_adapter(request)
        client = oauth2_adapter.get_client(request, app)

        auth_params = kwargs.pop("auth_params", None)
        if auth_params is None:
            auth_params = provider.get_auth_params()
        pkce_params = provider.get_pkce_params()
        code_verifier = pkce_params.pop("code_verifier", None)
        auth_params.update(pkce_params)

        scope = kwargs.pop("scope", None)
        if scope is None:
            scope = provider.get_scope()

        state_id = provider.stash_redirect_state(
            request,
            process=process,
            next_url=next_url,
            pkce_code_verifier=code_verifier,
            headless=True,
            phone=None,
        )

        client.state = state_id
        client.callback_url = next_url
        # Save the session and get the session token
        request.session.save()
        session_token = request.session.session_key

        return JsonResponse(
            {
                "redirect_url": client.get_redirect_url(
                    oauth2_adapter.authorize_url, scope, auth_params
                ),
                "session_token": session_token,
            },
            status=status.HTTP_200_OK,
        )


class ProviderCallbackAPIView(AllauthAPIView):
    """
    Custom endpoint to handle provider callback logic.
    This is used for non-browser clients to complete the OAuth flow.
    If successful, it stores the access token and returns status=200 with no data.
    """

    handle_json_input = False

    @csrf_exempt
    def dispatch(self, request, *args, **kwargs):
        data = json.loads(request.body.decode("utf-8"))
        provider_id = data.get("provider_id")
        if not provider_id:
            return JsonResponse(
                {"error": "Provider ID is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        app: SocialApp = get_socialaccount_adapter(request).get_app(
            request, provider_id, client_id=data.get("client_id")
        )
        provider: OAuth2Provider = app.get_provider(request)
        oauth2_adapter = provider.get_oauth2_adapter(request)
        client = oauth2_adapter.get_client(request, app)
        self.adapter = oauth2_adapter

        state, resp = self._get_state(request, data.get("state"))
        if resp:
            return resp
        if "error" in data or "code" not in data:
            # Distinguish cancel from error
            auth_error = data.get("error", None)
            if auth_error == self.adapter.login_cancelled_error:
                error = AuthError.CANCELLED
            else:
                error = AuthError.UNKNOWN
            return JsonResponse(
                {
                    "error": str(error),
                    "message": "Authentication cancelled or failed.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        app = provider.app
        client = self.adapter.get_client(self.request, app)
        client.callback_url = state["next"]

        try:
            access_token = client.get_access_token(
                data.get("code"), pkce_code_verifier=data.get("pkce_code_verifier")
            )
            token = self.adapter.parse_token(access_token)
            if app.pk:
                token.app = app
            login = self.adapter.complete_login(request, app, token, response=access_token)
            login.token = token
            login.state = state
            response = complete_social_login(request, login)
            if isinstance(response, JsonResponse):
                return response
            return AuthenticationResponse.from_response(request, response)
        except (
            PermissionDenied,
            OAuth2Error,
            RequestException,
            ProviderException,
        ):
            return JsonResponse(
                {
                    "error": str(AuthError.UNKNOWN),
                    "message": "Authentication failed.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    def _get_state(self, request, state_id):
        if self.adapter.supports_state and state_id:
            state = statekit.unstash_state(request, state_id)
        else:
            state = statekit.unstash_last_state(request)
        if state is None:
            return None, JsonResponse(
                {
                    "error": str(AuthError.UNKNOWN),
                    "message": "Authentication failed.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return state, None


class ProviderSignupView(AllauthAPIView):
    input_class = SignupInput

    def handle(self, request, *args, **kwargs):
        self.sociallogin = flows.signup.get_pending_signup(self.request)
        if not self.sociallogin:
            return ConflictResponse(request)
        if not get_socialaccount_adapter().is_open_for_signup(request, self.sociallogin):
            return ForbiddenResponse(request)
        return super().handle(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        return SocialLoginResponse(request, self.sociallogin)

    def post(self, request, *args, **kwargs):
        response = flows.signup.signup_by_form(self.request, self.sociallogin, self.input)
        return AuthenticationResponse.from_response(request, response)

    def get_input_kwargs(self):
        return {"sociallogin": self.sociallogin}
