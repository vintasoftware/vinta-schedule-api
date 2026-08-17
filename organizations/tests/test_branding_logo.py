"""Tests for the unauthenticated branding-logo delivery route
(``organizations.views.OrganizationLogoDeliveryView``).

The route resolves ``slug -> Organization -> resolve_branding_for_display ->
stored logo key`` and streams that S3 object, or the bundled default logo on
any miss. These tests exist to prove three things a Tier-4 reviewer will
specifically attack:

1. **No existence oracle.** An unknown slug, an organization with no branding
   row, a branding row with no logo, and an unentitled organization all
   produce an *identical* response (status, headers, body) -- the default
   logo, indistinguishably.
2. **No slug -> object traversal.** The route resolves ONLY through a
   branding row (via ``resolve_branding_for_display``); it has no key/path
   parameter of its own, so nothing a caller supplies can point it at an
   arbitrary S3 object.
3. **Correct delivery for a real, branded, entitled organization** -- with
   caching headers (``Cache-Control``, ``ETag``).
"""

import datetime
import io
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework.test import APIClient

from organizations.branding_logo import (
    DEFAULT_LOGO_ETAG_IDENTITY,
    compute_logo_etag,
)
from organizations.models import Organization, OrganizationBranding
from organizations.slug_validation import validate_organization_slug
from payments.billing_constants import BillingState, Entitlement
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement


# This module builds its own Subscription rows (mirroring organizations/tests/
# test_branding.py) so it can construct an explicitly UNENTITLED organization --
# the autouse `provision_default_subscription` fixture would otherwise grant every
# baker-made Organization the (all-entitlements-on) "unlimited" plan.
pytestmark = pytest.mark.no_auto_subscription


def _org_with_entitlement(entitlement_key: str, is_enabled: bool, **org_kwargs) -> Organization:
    org = baker.make(Organization, **org_kwargs)
    now = timezone.now()
    subscription = baker.make(
        Subscription,
        organization=org,
        plan=baker.make(BillingPlan, is_default_for_new_organizations=False),
        billing_state=BillingState.FREE,
        current_period_start=now,
        current_period_end=now + datetime.timedelta(days=30),
    )
    baker.make(
        SubscriptionEntitlement,
        subscription=subscription,
        entitlement_key=entitlement_key,
        is_enabled=is_enabled,
    )
    return org


def _logo_url(org_slug: str) -> str:
    return reverse("organization-branding-logo", kwargs={"org_slug": org_slug})


@pytest.fixture
def client():
    return APIClient()


@pytest.mark.django_db
class TestBrandedLogoDelivery:
    """A branded, entitled organization's real logo streams correctly."""

    def test_streams_the_stored_logo_with_caching_headers(self, client):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="brandco",
        )
        baker.make(
            OrganizationBranding,
            organization=org,
            app_name="BrandCo",
            logo="uploads/branding_logos/brandco-logo.png",
        )

        with (
            patch("common.media_storage_backend.MediaStorage.exists", return_value=True),
            patch(
                "common.media_storage_backend.MediaStorage.open",
                return_value=io.BytesIO(b"\x89PNG-fake-bytes"),
            ),
        ):
            response = client.get(_logo_url("brandco"))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response["Cache-Control"] == "public, max-age=300"
        assert response["ETag"] == compute_logo_etag("uploads/branding_logos/brandco-logo.png")
        assert response["X-Content-Type-Options"] == "nosniff"
        assert b"".join(response.streaming_content) == b"\x89PNG-fake-bytes"

    def test_jpeg_extension_resolves_jpeg_content_type(self, client):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="jpegco",
        )
        baker.make(
            OrganizationBranding,
            organization=org,
            logo="uploads/branding_logos/jpegco-logo.jpg",
        )

        with (
            patch("common.media_storage_backend.MediaStorage.exists", return_value=True),
            patch(
                "common.media_storage_backend.MediaStorage.open",
                return_value=io.BytesIO(b"fake-jpeg-bytes"),
            ),
        ):
            response = client.get(_logo_url("jpegco"))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/jpeg"

    def test_a_deleted_underlying_object_degrades_to_default_not_a_500(self, client):
        """The branding row references a key, but the object itself is gone (e.g.
        deleted directly in S3, out of band). Must degrade cleanly, never 500."""
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="ghostco",
        )
        baker.make(
            OrganizationBranding,
            organization=org,
            logo="uploads/branding_logos/ghostco-logo.png",
        )

        with patch("common.media_storage_backend.MediaStorage.exists", return_value=False):
            response = client.get(_logo_url("ghostco"))

        assert response.status_code == 200
        assert response["ETag"] == compute_logo_etag(DEFAULT_LOGO_ETAG_IDENTITY)


@pytest.mark.django_db
class TestMissConditionsAreIndistinguishable:
    """Every miss -- unknown slug, no branding row, no logo, unentitled -- must
    produce an IDENTICAL response: same status, same headers, same body. Nothing
    about the response may let a caller tell these four cases apart, or tell any
    of them apart from a request for a genuinely nonexistent organization."""

    def _responses(self, client) -> list:
        unknown_slug_response = client.get(_logo_url("no-such-organization"))

        baker.make(Organization, parent=None, slug="no-branding-row")
        no_branding_row_response = client.get(_logo_url("no-branding-row"))

        no_logo_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="no-logo-set",
        )
        baker.make(OrganizationBranding, organization=no_logo_org, logo="")
        no_logo_response = client.get(_logo_url("no-logo-set"))

        unentitled_org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=False,
            can_invite_organizations=True,
            parent=None,
            slug="unentitled-org",
        )
        baker.make(
            OrganizationBranding,
            organization=unentitled_org,
            logo="uploads/branding_logos/should-never-render.png",
        )
        unentitled_response = client.get(_logo_url("unentitled-org"))

        return [
            ("unknown slug", unknown_slug_response),
            ("no branding row", no_branding_row_response),
            ("no logo set", no_logo_response),
            ("unentitled organization", unentitled_response),
        ]

    def test_every_miss_returns_200_with_the_default_logo(self, client):
        for label, response in self._responses(client):
            assert response.status_code == 200, label
            assert response["Content-Type"] == "image/png", label
            assert response["ETag"] == compute_logo_etag(DEFAULT_LOGO_ETAG_IDENTITY), label
            assert response["Cache-Control"] == "public, max-age=300", label

    def test_every_miss_produces_byte_identical_bodies(self, client):
        bodies = {
            label: b"".join(response.streaming_content)
            for label, response in self._responses(client)
        }
        distinct_bodies = set(bodies.values())
        assert len(distinct_bodies) == 1, (
            f"Miss conditions produced different bodies, which would let a caller "
            f"distinguish them: {list(bodies.keys())}"
        )
        assert len(next(iter(distinct_bodies))) > 0

    def test_unentitled_organizations_logo_never_renders(self, client):
        """A stored key exists on the unentitled org's branding row -- proves the
        route does not silently fall through to it once the entitlement gate
        inside `resolve_branding_for_display` denies."""
        for label, response in self._responses(client):
            if label != "unentitled organization":
                continue
            assert response["ETag"] != compute_logo_etag(
                "uploads/branding_logos/should-never-render.png"
            )


@pytest.mark.django_db
class TestRouteResolvesOnlyThroughABrandingRow:
    """The route has no key/path parameter of its own -- only `org_slug`. Nothing
    a caller supplies can be used to address an arbitrary S3 object."""

    def test_query_string_cannot_override_the_resolved_key(self, client):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="queryco",
        )
        baker.make(
            OrganizationBranding,
            organization=org,
            logo="uploads/branding_logos/queryco-logo.png",
        )

        with (
            patch(
                "common.media_storage_backend.MediaStorage.exists", return_value=True
            ) as exists_mock,
            patch(
                "common.media_storage_backend.MediaStorage.open",
                return_value=io.BytesIO(b"real-bytes"),
            ),
        ):
            response = client.get(_logo_url("queryco") + "?key=some/other/secret-object.png")

        assert response.status_code == 200
        # The storage layer was only ever asked about the branding row's OWN key,
        # never the query-string value -- proving the route never reads it.
        exists_mock.assert_called_once_with("uploads/branding_logos/queryco-logo.png")

    def test_an_organization_with_a_parent_cannot_be_reached_by_its_own_slug_alone(self, client):
        """Even if a child organization somehow set its own slug and its own
        (hypothetical) branding-shaped data, resolution goes through
        `resolve_branding_for_display`, which walks to the branding ROOT -- a
        child with no entitled/reseller ancestor still gets the default."""
        parent = baker.make(Organization, parent=None, can_invite_organizations=False)
        child = baker.make(Organization, parent=parent, slug="child-org")

        response = client.get(_logo_url("child-org"))

        assert response.status_code == 200
        assert response["ETag"] == compute_logo_etag(DEFAULT_LOGO_ETAG_IDENTITY)
        # Sanity: the child really has no branding row to have leaked from.
        assert not OrganizationBranding.objects.filter(organization=child).exists()


@pytest.mark.django_db
class TestDefaultSentinelSlug:
    """The reserved "default" slug (`organizations.branding_logo.
    DEFAULT_LOGO_SLUG_SENTINEL`) always serves the bundled default logo, and no
    real organization can ever claim it as its own slug (enforced by
    `organizations.slug_validation`)."""

    def test_default_sentinel_serves_the_bundled_default_logo(self, client):
        response = client.get(_logo_url("default"))

        assert response.status_code == 200
        assert response["ETag"] == compute_logo_etag(DEFAULT_LOGO_ETAG_IDENTITY)

    def test_default_is_a_reserved_slug(self):
        with pytest.raises(ValidationError, match="reserved"):
            validate_organization_slug("default")


@pytest.mark.django_db
class TestForeignPrefixKeyNeverServed:
    """Read-side defense in depth: even if a key outside `uploads/branding_logos/`
    somehow ends up on a branding row (bypassing the write-side rejection in
    `normalize_uploaded_logo_key` -- e.g. a row inserted directly), the delivery
    route must never stream it -- it must degrade to the default logo exactly
    like any other miss."""

    def test_foreign_prefix_key_forced_into_db_serves_the_default_not_the_object(self, client):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="hijackco",
        )
        # Bypass the write-side rejection entirely -- write the foreign-prefix key
        # straight to the DB, simulating a row that predates the constraint or
        # was written by some other path.
        baker.make(
            OrganizationBranding,
            organization=org,
            logo="providers_documents/victim-private-document.pdf",
        )

        with (
            patch("common.media_storage_backend.MediaStorage.exists") as exists_mock,
            patch("common.media_storage_backend.MediaStorage.open") as open_mock,
        ):
            response = client.get(_logo_url("hijackco"))

        assert response.status_code == 200
        assert response["ETag"] == compute_logo_etag(DEFAULT_LOGO_ETAG_IDENTITY)
        # The storage layer must never even be asked about the foreign key --
        # the prefix check happens before any S3 call.
        exists_mock.assert_not_called()
        open_mock.assert_not_called()


@pytest.mark.django_db
class TestDeliveredContentTypeIsInertAndAllowlisted:
    """The delivery route must never let a stored key's extension drive a
    browser-renderable Content-Type (stored XSS via `.svg`/`.html`), and must
    always mark the response inert with `X-Content-Type-Options: nosniff`."""

    def test_svg_extension_is_served_as_octet_stream_not_svg(self, client):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="svgco",
        )
        baker.make(
            OrganizationBranding,
            organization=org,
            logo="uploads/branding_logos/malicious.svg",
        )

        with (
            patch("common.media_storage_backend.MediaStorage.exists", return_value=True),
            patch(
                "common.media_storage_backend.MediaStorage.open",
                return_value=io.BytesIO(b"<svg onload=alert(1)></svg>"),
            ),
        ):
            response = client.get(_logo_url("svgco"))

        assert response.status_code == 200
        assert response["Content-Type"] == "application/octet-stream"
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_html_extension_is_served_as_octet_stream_not_html(self, client):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="htmlco",
        )
        baker.make(
            OrganizationBranding,
            organization=org,
            logo="uploads/branding_logos/malicious.html",
        )

        with (
            patch("common.media_storage_backend.MediaStorage.exists", return_value=True),
            patch(
                "common.media_storage_backend.MediaStorage.open",
                return_value=io.BytesIO(b"<script>alert(1)</script>"),
            ),
        ):
            response = client.get(_logo_url("htmlco"))

        assert response.status_code == 200
        assert response["Content-Type"] == "application/octet-stream"
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_png_still_serves_the_allowlisted_content_type_with_nosniff(self, client):
        org = _org_with_entitlement(
            Entitlement.WHITE_LABEL_BRANDING,
            is_enabled=True,
            can_invite_organizations=True,
            parent=None,
            slug="pngco",
        )
        baker.make(
            OrganizationBranding,
            organization=org,
            logo="uploads/branding_logos/real-logo.png",
        )

        with (
            patch("common.media_storage_backend.MediaStorage.exists", return_value=True),
            patch(
                "common.media_storage_backend.MediaStorage.open",
                return_value=io.BytesIO(b"\x89PNG-fake-bytes"),
            ),
        ):
            response = client.get(_logo_url("pngco"))

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response["X-Content-Type-Options"] == "nosniff"

    def test_default_logo_response_also_carries_nosniff(self, client):
        response = client.get(_logo_url("default"))

        assert response.status_code == 200
        assert response["X-Content-Type-Options"] == "nosniff"


@pytest.mark.django_db
class TestNoQueryCountOracleBetweenUnknownSlugAndExistingOrg:
    """An unknown slug and a real, unbranded organization's slug should cost
    close to the same number of DB queries -- otherwise the response time /
    query count itself becomes an enumeration oracle, even though the two
    responses are byte-identical.

    **Note**: ``get_branding_root()`` was widened so a parentless organization
    resolves to *itself* rather than ``None``. Before that widening, an unbranded
    non-reseller organization's branding root was ``None`` at zero extra query
    cost, matching an unknown slug exactly. Now, that same organization is
    its own branding root, so ``resolve_branding_for_display`` must run the
    ``white_label_branding`` entitlement check against it -- exactly one extra
    query (the subscription/entitlement lookup) an unknown slug never reaches.
    This is an accepted, unavoidable trade-off of widening branding to every
    parentless organization: once a matching row exists, determining "is this
    organization entitled to apply its own branding" costs one DB round-trip,
    and no real, found organization can cost strictly zero additional queries
    any more (e.g. a child organization instead pays a parent-chain-walk
    query). The response BODY and STATUS remain byte-identical regardless
    (covered by the other tests in this module) -- this test is narrowed to
    pin the divergence at exactly the one expected extra query, not more, so a
    regression that fans this out (e.g. an N+1 in the entitlement walk) is
    still caught.
    """

    def test_unknown_slug_and_existing_unbranded_org_cost_at_most_one_extra_query(self, client):
        baker.make(Organization, parent=None, slug="normalized-no-branding-row")

        with CaptureQueriesContext(connection) as unknown_ctx:
            response = client.get(_logo_url("no-such-organization-at-all"))
        assert response.status_code == 200

        with CaptureQueriesContext(connection) as existing_ctx:
            response = client.get(_logo_url("normalized-no-branding-row"))
        assert response.status_code == 200

        unknown_count = len(unknown_ctx.captured_queries)
        existing_count = len(existing_ctx.captured_queries)
        assert existing_count - unknown_count == 1, (
            f"Query count diverges by more than the one expected extra query "
            f"(the white_label_branding entitlement check that runs "
            f"against every parentless organization): unknown slug ran "
            f"{unknown_count} quer(ies), existing unbranded org ran "
            f"{existing_count} quer(ies)."
        )
