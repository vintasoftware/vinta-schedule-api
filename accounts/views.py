import json
import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from allauth.headless.base.response import (
    AuthenticationResponse,
)
from allauth.headless.base.views import APIView as AllauthAPIView
from allauth.headless.socialaccount.forms import RedirectToProviderForm
from allauth.socialaccount.adapter import get_adapter as get_socialaccount_adapter
from allauth.socialaccount.helpers import (
    complete_social_login,
)
from allauth.socialaccount.internal import statekit
from allauth.socialaccount.models import SocialApp
from allauth.socialaccount.providers.base import ProviderException
from allauth.socialaccount.providers.base.constants import AuthError
from allauth.socialaccount.providers.oauth2.client import (
    OAuth2Error,
)
from allauth.socialaccount.providers.oauth2.provider import OAuth2Provider
from requests.exceptions import RequestException
from rest_framework import status

from organizations.models import get_active_organization_membership, resolve_branding_for_display


logger = logging.getLogger(__name__)


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
                return self._with_post_auth_destination(request, response)
            return self._with_post_auth_destination(
                request, AuthenticationResponse.from_response(request, response)
            )
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

    def _with_post_auth_destination(
        self, request: HttpRequest, response: JsonResponse
    ) -> JsonResponse:
        """Merge the resolved post-authentication destination into a completed-login response.

        No-ops (returns ``response`` unchanged) unless ``request.user`` is authenticated:
        a completed login is the only case with an acting organization to resolve
        branding for. An interim response (a pending stage, a cancelled/failed
        attempt) leaves the request unauthenticated, so it passes through untouched.

        The destination is resolved *only* from the acting organization's stored
        branding (``organizations.models.resolve_branding_for_display``) -- never
        from ``state["next"]``, a query parameter, or a header. ``state["next"]``
        keeps serving as the OAuth ``callback_url`` for the token exchange above
        (a protocol requirement); it plays no part in this method. That separation
        is what removes the open-redirect surface the old caller-supplied-allowlist
        design carried.
        """
        if not request.user.is_authenticated:
            return response

        # The OAuth callback has no selected-org concept: a user with multiple
        # active memberships deterministically resolves to their oldest active
        # membership's org via get_active_organization_membership. Intentional
        # and safe -- all candidate orgs are the user's own.
        membership = get_active_organization_membership(request.user)
        organization = membership.organization if membership else None
        branding = resolve_branding_for_display(organization)

        if branding is not None and branding.redirect_url:
            destination = branding.redirect_url
            destination_source = "configured"
        else:
            destination = settings.FRONTEND_BASE_URL
            destination_source = "dashboard_fallback"

        logger.info(
            "Post-authentication destination resolved",
            extra={
                "organization_id": organization.pk if organization else None,
                "destination_source": destination_source,
            },
        )

        # Mutate the response body in place rather than building a fresh
        # JsonResponse: this preserves cookies and any other header not covered
        # by the header-copy loop it would otherwise take to replicate them.
        payload = json.loads(response.content)
        payload["destination"] = destination
        response.content = json.dumps(payload).encode("utf-8")
        return response

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
