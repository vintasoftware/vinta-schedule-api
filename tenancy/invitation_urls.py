"""Builds the invitation accept URL (Organization Auth-Area Branding plan, Phase 5
amendment -- 2026-08-06).

Phase 5 resolved branding into the invitation *email's content* (app name, logo,
colors -- see ``tenancy.notification_contexts``) but never threaded the
branding root's slug into the accept link itself, so every invitation -- branded
or not -- kept pointing at the same slug-less URL. The SPA's accept-invite page
has no other way to discover which organization a bare token belongs to before
the user authenticates (there is no unauthenticated "resolve invitation by
token" endpoint), so without the slug in the URL it can never call
``brandingForTenant`` to render that organization's identity.

Split into its own module for the same reason as ``tenancy.branding_logo``:
the email-send path (``tenancy.services.OrganizationService.
invite_user_to_organization``) and the public-API path (``public_api.mutations.
Mutation.create_invitation``) must build byte-for-byte the same URL shape from
the same inputs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings


if TYPE_CHECKING:
    from tenancy.models import Organization


def build_invitation_accept_url(branding_root: Organization | None, token: str) -> str:
    """Accept-invitation URL for ``token``.

    ``branding_root`` must be the branding root -- ``resolve_branding_for_display
    (invitation.organization).organization`` when a branding row resolved for the
    invitation's organization, ``None`` otherwise -- mirroring
    ``tenancy.branding_logo.build_logo_delivery_url``'s contract. This is
    what makes an invitation from a reseller's child organization carry the
    reseller's slug (not the child's, which has none), so the accept page
    resolves the reseller's branding instead of silently falling back to our
    default identity.

    Returns the plain, slug-less ``account_accept_invitation`` template when
    ``branding_root`` is ``None``, has no slug, or the branded template is not
    configured -- the exact URL every invitation got before this change, so an
    unbranded organization's invitation stays byte-for-byte identical.
    """
    urls = getattr(settings, "HEADLESS_FRONTEND_URLS", {})
    if branding_root is not None and branding_root.slug:
        branded_template = urls.get("account_accept_invitation_branded", "")
        if branded_template:
            return branded_template.format(token=token, org_slug=branding_root.slug)
    return urls.get("account_accept_invitation", "").format(token=token)
