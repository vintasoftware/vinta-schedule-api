"""Request/response middleware for the authentication surface."""

import json
import logging
from http import HTTPStatus

from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse

from accounts.post_auth_destination import inject_post_auth_destination


logger = logging.getLogger(__name__)


class PostAuthDestinationMiddleware:
    """Add the resolved post-authentication destination to every completed
    allauth-headless authentication response.

    ``accounts.views.ProviderCallbackAPIView`` (the social OAuth callback)
    resolves this destination itself, but it is only one of the ways a session
    is established. An email/password signup finishes at
    ``POST /auth/{client}/v1/auth/email/verify``, a returning user at
    ``.../auth/login``, an SMS-verified user at ``.../auth/phone/verify`` -- all
    of them served by allauth's own headless views, which know nothing about
    organization branding. Without this middleware those responses carry no
    destination and the SPA has nothing to navigate to, so a user who signed up
    with an email and a password never reaches their organization's configured
    ``redirect_url``.

    Rather than subclassing and re-routing each of allauth's views, this hooks
    the shape they all share: the headless API envelope
    (``{"status": 200, "data": {"user": ...}, "meta": {"is_authenticated": true}}``).
    That covers today's endpoints and any authentication flow allauth adds
    later. No other surface in this project answers in that envelope -- DRF and
    Strawberry both have their own shapes -- so nothing else is touched.

    The authenticated user is read from the response body rather than from
    ``request.user``: allauth's
    ``headless.internal.authkit.authentication_context`` swaps in the headless
    session for the duration of the view and restores ``request.user`` on the
    way out, so by the time this middleware runs, a request that just logged
    someone in looks anonymous again. The body is our own server-generated
    envelope -- the same one carrying the session and access tokens -- so
    trusting its user id is no weaker than trusting those.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        user_id = self._authenticated_user_id(response)
        if user_id is None:
            return response

        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            # The envelope named a user that no longer exists (deleted between
            # the view and here). The field is still written -- it resolves to
            # the dashboard fallback -- so "a completed authentication always
            # carries a destination" holds without exception, which is what the
            # published schema promises.
            logger.warning("Authentication response names an unknown user id %s", user_id)

        return inject_post_auth_destination(response, user)

    @staticmethod
    def _authenticated_user_id(response: HttpResponse) -> str | int | None:
        """The id of the user an allauth headless envelope reports as authenticated,
        or ``None`` when *response* is not such an envelope.

        Deliberately conservative: a streaming response has no ``content`` to
        parse, a non-JSON content type is not an envelope, and a body that does
        not parse into the exact envelope shape is left alone rather than
        guessed at. Only ``200`` qualifies -- a ``401`` reauthentication response
        also reports ``is_authenticated: true``, but no session was established
        there, so there is nowhere to send anyone.
        """
        if getattr(response, "streaming", False):
            return None
        if response.status_code != HTTPStatus.OK:
            return None
        if not response.get("Content-Type", "").startswith("application/json"):
            return None

        try:
            payload = json.loads(response.content)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        meta = payload.get("meta")
        if not isinstance(meta, dict) or meta.get("is_authenticated") is not True:
            return None

        data = payload.get("data")
        user = data.get("user") if isinstance(data, dict) else None
        return user.get("id") if isinstance(user, dict) else None
