"""Tests for tenancy.invitation_urls.build_invitation_accept_url.

Organization Auth-Area Branding plan, Phase 5 amendment (2026-08-06): the
invitation accept link must carry the branding root's slug so the SPA's
accept-invite page can resolve that organization's branding before the user
authenticates -- see the amendment note in the implementation plan for why
this was missing from the original Phase 5 delivery.
"""

from model_bakery import baker

from tenancy.invitation_urls import build_invitation_accept_url
from tenancy.models import Organization
from tenancy.tests.helpers import clear_organization_slug


ACCEPT_URLS = {
    "account_accept_invitation": "https://frontend.example.com/auth/accept-invite/?token={token}",
    "account_accept_invitation_branded": (
        "https://frontend.example.com/o/{org_slug}/auth/accept-invite/?token={token}"
    ),
}


class TestBuildInvitationAcceptUrl:
    def test_no_branding_root_uses_the_plain_template(self, settings):
        settings.HEADLESS_FRONTEND_URLS = ACCEPT_URLS

        url = build_invitation_accept_url(None, "tok123")

        assert url == "https://frontend.example.com/auth/accept-invite/?token=tok123"

    def test_branding_root_with_no_slug_uses_the_plain_template(self, settings, db):
        settings.HEADLESS_FRONTEND_URLS = ACCEPT_URLS
        org = clear_organization_slug(baker.make(Organization))

        url = build_invitation_accept_url(org, "tok123")

        assert url == "https://frontend.example.com/auth/accept-invite/?token=tok123"

    def test_branding_root_with_a_slug_uses_the_branded_template(self, settings, db):
        settings.HEADLESS_FRONTEND_URLS = ACCEPT_URLS
        org = baker.make(Organization, slug="brandco")

        url = build_invitation_accept_url(org, "tok123")

        assert url == "https://frontend.example.com/o/brandco/auth/accept-invite/?token=tok123"

    def test_missing_branded_template_falls_back_to_the_plain_one(self, settings, db):
        """A deploy that has not rolled out the branded template (e.g. a settings
        module amended without it) must still produce a working, byte-for-byte
        identical URL to what unbranded organizations got before this change --
        never a KeyError, never an unformatted string."""
        settings.HEADLESS_FRONTEND_URLS = {
            "account_accept_invitation": ACCEPT_URLS["account_accept_invitation"],
        }
        org = baker.make(Organization, slug="brandco")

        url = build_invitation_accept_url(org, "tok123")

        assert url == "https://frontend.example.com/auth/accept-invite/?token=tok123"
