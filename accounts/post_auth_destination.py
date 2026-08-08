"""Where a just-authenticated user should land, resolved server-side.

The destination is read exclusively from the acting organization's stored
branding (``organizations.models.resolve_branding_for_display``) -- never from a
``next``/``callback_url`` parameter, a header, or anything else the caller
controls. That is the whole point of the design: there is no caller-supplied
redirect target, so there is no open-redirect surface to validate away (see the
Organization Auth-Area Branding plan, Phase 2a/7).

Both the social OAuth callback (``accounts.views.ProviderCallbackAPIView``) and
every allauth-headless authentication response (via
``accounts.middlewares.PostAuthDestinationMiddleware``) answer with the same
field, resolved the same way -- an email/password signup that finishes at the
verify-email endpoint lands exactly where a social login of the same user would.
"""

import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from organizations.models import get_active_organization_membership, resolve_branding_for_display
from users.models import User


logger = logging.getLogger(__name__)

#: Top-level key carrying the destination in an authentication response body.
#: Top-level (a sibling of allauth's ``data``/``meta``) rather than nested in
#: ``data``: it describes the *response*, not the authenticated user, and this
#: is the shape the SPA already reads from the social callback.
DESTINATION_FIELD = "destination"


def dashboard_url() -> str:
    """Our own signed-in app: the destination for a user whose organization has
    configured none of its own.

    ``FRONTEND_BASE_URL`` alone would land them on the public landing page, so
    the dashboard path is joined onto it here rather than assumed. Joined at call
    time because ``staging``/``production`` reassign ``FRONTEND_BASE_URL`` after
    ``base`` is imported.
    """
    return f"{settings.FRONTEND_BASE_URL}{settings.FRONTEND_DASHBOARD_PATH}"


def resolve_post_auth_destination(user: User | None) -> str:
    """The absolute URL *user* should be sent to now that they are authenticated.

    The acting organization's configured ``redirect_url`` when it has one (and
    holds the branding entitlement), our own dashboard (``dashboard_url``)
    otherwise -- including for a user with no membership at all, e.g. one still
    gated on onboarding.

    Always returns a URL, never an empty string or ``None``, so a caller can hand
    the result straight to a navigation. ``user`` may be ``None`` (the row went
    away between the view and the response) and takes the same dashboard
    fallback as a membership-less user -- there is no case where a completed
    authentication answers with nowhere to go.

    There is no selected-org concept at authentication time: a user with several
    active memberships resolves to their oldest one via
    ``get_active_organization_membership``. Deterministic and safe -- every
    candidate organization is one the user belongs to.
    """
    membership = get_active_organization_membership(user) if user is not None else None
    organization = membership.organization if membership else None
    branding = resolve_branding_for_display(organization)

    if branding is not None and branding.redirect_url:
        destination = branding.redirect_url
        destination_source = "configured"
    else:
        destination = dashboard_url()
        destination_source = "dashboard_fallback"

    logger.info(
        "Post-authentication destination resolved",
        extra={
            "organization_id": organization.pk if organization else None,
            "destination_source": destination_source,
        },
    )
    return destination


def inject_post_auth_destination(response: HttpResponse, user: User | None) -> HttpResponse:
    """Write the destination resolved for *user* into a completed-authentication
    response body.

    Unconditional: callers reach here having already established that the
    response represents a completed authentication, and
    ``resolve_post_auth_destination`` always answers, so every such response
    carries the field. That is what lets the published schema mark it required
    (see ``accounts.management.commands.export_allauth_schema``).

    Idempotent -- a body that already carries the field is left alone, so a view
    that injects it explicitly and the middleware that injects it globally can
    both run without the second re-resolving what the first already answered.

    The body is mutated in place rather than rebuilt: that preserves cookies and
    every other header a fresh ``JsonResponse`` would drop.
    """
    payload = json.loads(response.content)
    if DESTINATION_FIELD in payload:
        return response

    payload[DESTINATION_FIELD] = resolve_post_auth_destination(user)
    response.content = json.dumps(payload).encode("utf-8")
    return response


def with_post_auth_destination(request: HttpRequest, response: HttpResponse) -> HttpResponse:
    """``inject_post_auth_destination`` for a caller running inside the request,
    keyed on ``request.user``.

    A no-op when the request is not authenticated: an interim response (a pending
    verification stage, a cancelled or failed attempt) is not a completed
    authentication and gets no destination.

    Only ``ProviderCallbackAPIView`` uses this. The middleware cannot: by the
    time a response-phase middleware runs, allauth's
    ``headless.internal.authkit.authentication_context`` has restored
    ``request.user`` to whoever it was *before* the login -- anonymous, for the
    signup and verify-email flows it exists to serve -- so it identifies the user
    from the response envelope instead.
    """
    if not request.user.is_authenticated:
        return response
    return inject_post_auth_destination(response, request.user)
