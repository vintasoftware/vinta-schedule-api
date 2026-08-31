import json
import logging
from http import HTTPStatus

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from allauth.headless.account.views import SignupView as AllauthSignupView
from allauth.headless.base.response import (
    AuthenticationResponse,
)
from allauth.headless.base.views import APIView as AllauthAPIView
from allauth.headless.internal.restkit.response import APIResponse
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

from accounts.exceptions import VerificationEmailUndeliverableError
from accounts.post_auth_destination import with_post_auth_destination


logger = logging.getLogger(__name__)


class AtomicSignupView(AllauthSignupView):
    """allauth's headless signup, made all-or-nothing.

    Two things go wrong without it, both invisible from the client.

    **The account outlives its verification email.** ``SignupView.post`` creates the
    user, then sends the confirmation as a later step. If that send fails, the user row
    is already committed -- and since ``ACCOUNT_EMAIL_VERIFICATION`` is mandatory, the
    account is unusable until a code from that email is typed back. The visitor is left
    with an address they can neither verify nor register again: a second attempt is
    answered by enumeration prevention, which starts a verification stage for the
    existing account and sends no new code. Running the whole POST in one transaction
    means a failure leaves no trace and trying again works.

    **The failure has no shape.** ``VerificationEmailUndeliverableError`` out of the
    adapter is not an ``ImmediateHttpResponse``, so nothing in allauth handles it and
    Django answers with a 500 HTML page -- to a caller that parses JSON. This answers in
    allauth's own envelope (``{"status": ..., "errors": [...]}``) so a client's existing
    error mapper renders the sentence with no special case.

    Registered ahead of ``allauth.headless.urls`` in ``accounts/urls.py``, the way the
    two OAuth endpoints below already are.
    """

    def post(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        try:
            with transaction.atomic():
                return super().post(request, *args, **kwargs)
        except VerificationEmailUndeliverableError as error:
            # The `atomic` block has already rolled the account back by the time this
            # runs -- the exception leaving it is what triggers the rollback.
            logger.error(
                "Refusing the signup: its verification email could not be sent. "
                "The account was rolled back so the address stays available."
            )
            return APIResponse(
                request,
                errors=[{"code": error.code, "message": error.message}],
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )


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
    ) -> HttpResponse:
        """Merge the resolved post-authentication destination into a completed-login response.

        Delegates to ``accounts.post_auth_destination``, shared with
        ``accounts.middlewares.PostAuthDestinationMiddleware`` so this callback and
        allauth's own headless authentication endpoints answer with the same field,
        resolved identically. The call is kept here rather than left to the
        middleware because this view builds its response by hand (including bodies
        ``complete_social_login`` hands back), and the destination is part of this
        endpoint's documented contract; the injection is idempotent, so the
        middleware seeing it again is a no-op.

        The destination is resolved *only* from the acting organization's stored
        branding -- never from ``state["next"]``, a query parameter, or a header.
        ``state["next"]`` keeps serving as the OAuth ``callback_url`` for the token
        exchange above (a protocol requirement); it plays no part here. That
        separation is what removes the open-redirect surface the old
        caller-supplied-allowlist design carried.
        """
        return with_post_auth_destination(request, response)

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
