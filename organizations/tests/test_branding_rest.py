import datetime
import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import (
    Organization,
    OrganizationBranding,
    OrganizationMembership,
    OrganizationRole,
)
from payments.billing_constants import BillingState, Entitlement
from payments.models import BillingPlan, Subscription, SubscriptionEntitlement


User = get_user_model()


def _make_unentitled_org(**org_kwargs) -> Organization:
    """A parentless organization whose subscription explicitly lacks
    ``white_label_branding`` -- the free-plan write-gate refusal. Callers must
    mark the test ``@pytest.mark.no_auto_subscription``: the suite's autouse
    fixture would otherwise grant every baker-made ``Organization`` the
    (all-entitlements-on) "unlimited" plan, making this state unreachable."""
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
        entitlement_key=Entitlement.WHITE_LABEL_BRANDING,
        is_enabled=False,
    )
    return org


BRANDING_URL = "/branding/"


def assert_logo_delivery_url(url: str) -> None:
    """``logo_url`` is always the logo delivery route's absolute URL -- see
    ``organizations.branding_logo.build_logo_delivery_url``. It is never the
    raw/signed S3 value that was written (round-trip identity is not the
    contract here -- ``organizations/tests/test_branding_logo.py`` covers
    slug-keyed resolution specifically)."""
    assert url.startswith("http"), url
    assert "/branding/logo/" in url, url


def assert_response_status_code(response, expected_status_code):
    """Assert response status code with helpful error message."""
    assert response.status_code == expected_status_code, (
        f"The status error {response.status_code} != {expected_status_code}\n"
        f"Response Payload: {json.dumps(response.json() if hasattr(response, 'json') and callable(response.json) else str(response.content))}"
    )


@pytest.fixture
def client():
    """REST API client."""
    return APIClient()


@pytest.fixture
def user():
    """Create a test user."""
    return baker.make(User)


@pytest.fixture
def reseller_org():
    """A reseller organization -- parentless (default) and entitled via the
    suite's autouse default subscription. Phase 3 widens the write gate with a
    THIRD condition (slug-set) on top of the pre-existing parentless+entitled
    pair, and a reseller is not exempt from it -- this fixture carries a slug
    so the pre-Phase-3 test bodies below keep passing. See
    ``TestResellerNowRequiresASlug`` for the explicit regression coverage of
    that behavior change."""
    return baker.make(Organization, can_invite_organizations=True, slug="acme-reseller")


@pytest.fixture
def reseller_org_without_slug():
    """Same as ``reseller_org`` but deliberately unslugged."""
    return baker.make(Organization, can_invite_organizations=True)


@pytest.fixture
def eligible_org():
    """A plain (non-reseller), parentless organization that is entitled (the
    suite's autouse default subscription) and has a slug -- the population
    Phase 3 widens the write gate to admit (spec Use-case 1's actor)."""
    return baker.make(Organization, can_invite_organizations=False, slug="eligible-org")


@pytest.fixture
def no_slug_org():
    """Parentless and entitled, but no slug -- the "one step away" refusal
    (spec: "Eligible org with no public identifier yet")."""
    return baker.make(Organization, can_invite_organizations=False, parent=None)


@pytest.fixture
def parented_org(eligible_org):
    """An organization with a parent -- refused on every write surface
    regardless of its own entitlement/slug state (spec Use-case 5)."""
    return baker.make(Organization, parent=eligible_org, slug="child-org")


@pytest.fixture
def reseller_org_admin(user, reseller_org):
    """Create an admin membership for the user in the reseller org."""
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=reseller_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def eligible_org_admin(user, eligible_org):
    """Create an admin membership for the user in the eligible (non-reseller) org."""
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=eligible_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def eligible_org_member(eligible_org):
    """A non-admin member of the eligible org."""
    member = baker.make(User)
    baker.make(
        OrganizationMembership,
        user=member,
        organization=eligible_org,
        role=OrganizationRole.MEMBER,
        is_active=True,
    )
    return member


@pytest.fixture
def no_slug_org_admin(user, no_slug_org):
    """Create an admin membership for the user in the no-slug org."""
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=no_slug_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def parented_org_admin(user, parented_org):
    """Create an admin membership for the user in the parented org."""
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=parented_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def reseller_org_member(reseller_org):
    """Create a non-admin member in the reseller org."""
    member = baker.make(User)
    baker.make(
        OrganizationMembership,
        user=member,
        organization=reseller_org,
        role=OrganizationRole.MEMBER,
        is_active=True,
    )
    return member


@pytest.mark.django_db
class TestOrganizationBrandingViewSet:
    """Test suite for OrganizationBrandingViewSet REST endpoints."""

    def test_retrieve_branding_not_configured_returns_404(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """GET /branding/ returns 404 when branding is not yet configured."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_retrieve_branding_success(self, client, user, reseller_org, reseller_org_admin):
        """GET /branding/ returns branding when configured."""
        _ = baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyScheduler",
            logo="uploads/branding_logos/logo.png",
            primary_color="#FF0000",
            secondary_color="#00FF00",
            support_email="support@example.com",
            redirect_url="https://example.com/return",
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == "MyScheduler"
        assert_logo_delivery_url(data["logo_url"])
        assert data["primary_color"] == "#FF0000"
        assert data["secondary_color"] == "#00FF00"
        assert data["support_email"] == "support@example.com"
        assert data["redirect_url"] == "https://example.com/return"

    def test_create_branding_via_put(self, client, user, reseller_org, reseller_org_admin):
        """PUT /branding/ creates branding (upsert)."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "logo_url": "uploads/branding_logos/logo.png",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "support_email": "support@example.com",
            "redirect_url": "https://example.com/return",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

        data = response.json()
        assert data["app_name"] == "MyScheduler"
        assert data["support_email"] == "support@example.com"
        assert_logo_delivery_url(data["logo_url"])

        # Verify branding was created in DB, storing the bare key (never a URL).
        branding = OrganizationBranding.objects.get(organization_id=reseller_org.id)
        assert branding.app_name == "MyScheduler"
        assert branding.logo.name == "uploads/branding_logos/logo.png"

    def test_update_branding_via_put_replaces_all_fields(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """PUT /branding/ replaces entire branding row (upsert)."""
        baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="OldName",
            logo="uploads/branding_logos/old-logo.png",
            primary_color="#0000FF",
            secondary_color="#FFFF00",
            support_email="old@example.com",
            redirect_url="https://old.example.com/return",
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "NewName",
            "logo_url": "uploads/branding_logos/new-logo.png",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "support_email": "new@example.com",
            "redirect_url": "https://new.example.com/return",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == "NewName"
        assert data["support_email"] == "new@example.com"
        assert_logo_delivery_url(data["logo_url"])

        # PUT is a full replace: the new key is stored, not the old one.
        branding = OrganizationBranding.objects.get(organization_id=reseller_org.id)
        assert branding.logo.name == "uploads/branding_logos/new-logo.png"

    def test_update_branding_via_patch_partial(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """PATCH /branding/ updates partial fields."""
        _ = baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="OriginalName",
            logo="uploads/branding_logos/original-logo.png",
            primary_color="#FF0000",
            secondary_color="#00FF00",
            support_email="original@example.com",
            redirect_url="https://original.example.com/return",
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "UpdatedName",
            "support_email": "updated@example.com",
        }

        response = client.patch(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == "UpdatedName"
        assert data["support_email"] == "updated@example.com"
        # Unchanged fields should remain
        assert_logo_delivery_url(data["logo_url"])
        assert data["primary_color"] == "#FF0000"

        # PATCH omitted logo_url entirely: the stored key is untouched.
        branding = OrganizationBranding.objects.get(organization_id=reseller_org.id)
        assert branding.logo.name == "uploads/branding_logos/original-logo.png"

    def test_patch_branding_when_not_configured_returns_404(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """PATCH /branding/ returns 404 when branding is not configured."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {"app_name": "NewName"}

        response = client.patch(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)

    def test_invalid_color_format_returns_400(self, client, user, reseller_org, reseller_org_admin):
        """Invalid color format (#RGB instead of #RRGGBB) returns 400."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#RGB",  # Invalid
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_valid_color_with_alpha_accepted(self, client, user, reseller_org, reseller_org_admin):
        """Color format #RRGGBBAA (with alpha) is accepted."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000AA",  # With alpha
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

    def test_invalid_redirect_url_returns_400(self, client, user, reseller_org, reseller_org_admin):
        """A malformed redirect_url value returns 400."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "not-a-valid-url",  # Invalid URL
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_non_https_redirect_url_returns_400(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """An http:// redirect_url is rejected -- HTTPS only."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "http://example.com/return",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_wildcard_redirect_url_returns_400(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """A redirect_url containing a wildcard character is rejected."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "https://*.example.com",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_path_prefix_redirect_url_returns_400(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """A redirect_url with a trailing-slash path-prefix pattern is rejected."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "https://example.com/callback/",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_valid_redirect_url_accepted(self, client, user, reseller_org, reseller_org_admin):
        """A plain HTTPS redirect_url is accepted."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "uploads/branding_logos/logo.png",
            "support_email": "support@example.com",
            "redirect_url": "https://example.com/return",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

    def test_non_reseller_org_without_a_slug_returns_403_for_the_slug_reason(
        self, client, user, no_slug_org, no_slug_org_admin
    ):
        """A plain (non-reseller) organization is no longer refused for lacking
        reseller status -- see ``TestBrandingWriteGateAllMethods`` for the full
        "eligible non-reseller org succeeds" coverage that is the point of this
        phase. It IS still refused here, but for the slug condition -- the
        response body must name the slug, not reseller status."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(no_slug_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        assert "slug" in str(response.json()).lower()
        assert "reseller" not in str(response.json()).lower()

    def test_non_admin_member_returns_403(self, client, reseller_org, reseller_org_member):
        """Non-admin member of reseller org gets 403."""
        client.force_authenticate(reseller_org_member)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_user_returns_401(self, client, reseller_org):
        """Unauthenticated user gets 401."""
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "logo_url": "",
            "support_email": "",
            "redirect_url": "",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_serializer_never_exposes_can_invite_organizations(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """can_invite_organizations is never exposed in the serializer."""
        _ = baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyScheduler",
            logo="",
            primary_color="",
            secondary_color="",
            support_email="",
            redirect_url="",
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert "can_invite_organizations" not in data
        assert "organization" not in data

    def test_only_acting_org_branding_accessible(
        self, client, user, reseller_org, reseller_org_admin
    ):
        """A user can only access their own org's branding, not another org's."""
        other_reseller = baker.make(Organization, can_invite_organizations=True)
        _ = baker.make(
            OrganizationBranding,
            organization=other_reseller,
            app_name="OtherApp",
            logo="",
            primary_color="",
            secondary_color="",
            support_email="",
            redirect_url="",
        )

        baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyApp",
            logo="",
            primary_color="",
            secondary_color="",
            support_email="",
            redirect_url="",
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        # Should see my branding, not the other org's
        assert data["app_name"] == "MyApp"

    def test_roundtrip_all_fields(self, client, user, reseller_org, reseller_org_admin):
        """Create and retrieve branding; all fields round-trip.

        ``logo_url`` is the one field that deliberately does NOT round-trip to
        the written value -- it always reads back as the logo delivery route's
        URL (see ``organizations.branding_logo.build_logo_delivery_url``), keyed
        by the acting org's slug, not the S3 key that was written.
        """
        reseller_org.slug = "clinic-scheduler"
        reseller_org.save(update_fields=["slug"])

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        original_payload = {
            "app_name": "ClinicScheduler",
            "logo_url": "uploads/branding_logos/clinic-logo.png",
            "primary_color": "#0066CC",
            "secondary_color": "#FF6633",
            "support_email": "help@clinic.example.com",
            "redirect_url": "https://clinic.example.com/return",
        }

        # Create branding
        response = client.put(BRANDING_URL, data=original_payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)

        # Retrieve and verify all fields match
        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["app_name"] == original_payload["app_name"]
        assert data["logo_url"].endswith("/branding/logo/clinic-scheduler/")
        assert data["primary_color"] == original_payload["primary_color"]
        assert data["secondary_color"] == original_payload["secondary_color"]
        assert data["support_email"] == original_payload["support_email"]
        assert data["redirect_url"] == original_payload["redirect_url"]

        # The written S3 key is what is actually stored -- never a URL.
        branding = OrganizationBranding.objects.get(organization_id=reseller_org.id)
        assert branding.logo.name == "uploads/branding_logos/clinic-logo.png"

    def test_delete_not_allowed(self, client, user, reseller_org, reseller_org_admin):
        """DELETE /branding/ returns 405 Method Not Allowed."""
        baker.make(
            OrganizationBranding,
            organization=reseller_org,
            app_name="MyScheduler",
            logo="",
            primary_color="",
            secondary_color="",
            support_email="",
            redirect_url="",
        )

        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        response = client.delete(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_multi_org_header_selects_correct_org(self, client):
        """Multi-org admin: X-Organization-Id header selects the named org's branding.

        Proves the BLOCKER fix: OrganizationBrandingView must be tenant-scoped via
        TenantScopedViewMixin so the header drives org selection.

        Setup: a single user is an ADMIN of TWO reseller orgs (reseller_a is older,
        reseller_b is newer).  Without the mixin the fallback would return the oldest
        membership (reseller_a) for every request, silently ignoring the header.
        """
        user = baker.make(User)
        reseller_a = baker.make(
            Organization, can_invite_organizations=True, slug="reseller-a-multi"
        )
        reseller_b = baker.make(
            Organization, can_invite_organizations=True, slug="reseller-b-multi"
        )

        # Create memberships: reseller_a first so it is the "oldest".
        baker.make(
            OrganizationMembership,
            user=user,
            organization=reseller_a,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=reseller_b,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        # Create distinct branding rows for each org.
        baker.make(
            OrganizationBranding,
            organization=reseller_a,
            app_name="BrandA",
            logo="",
            primary_color="",
            secondary_color="",
            support_email="",
            redirect_url="",
        )
        baker.make(
            OrganizationBranding,
            organization=reseller_b,
            app_name="BrandB",
            logo="",
            primary_color="",
            secondary_color="",
            support_email="",
            redirect_url="",
        )

        client.force_authenticate(user)

        # --- With header pointing to reseller_b → must return reseller_b's branding ---
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_b.id))
        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.json()["app_name"] == "BrandB", (
            "Expected BrandB but got a different app_name — "
            "the view is not using the X-Organization-Id header to select the org."
        )

        # --- With header pointing to reseller_a → must return reseller_a's branding ---
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_a.id))
        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_200_OK)
        assert response.json()["app_name"] == "BrandA"

        # --- PUT on reseller_b must NOT touch reseller_a's row ---
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_b.id))
        payload = {
            "app_name": "BrandB-updated",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }
        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        # reseller_a's row must be untouched
        reseller_a_branding = OrganizationBranding.objects.get(organization_id=reseller_a.id)
        assert reseller_a_branding.app_name == "BrandA", (
            "Writing reseller_b's branding must not touch reseller_a's row."
        )

    def test_multi_org_absent_header_returns_400(self, client):
        """Multi-org admin omitting X-Organization-Id gets 400 (not a silent fallback)."""
        user = baker.make(User)
        reseller_a = baker.make(
            Organization, can_invite_organizations=True, slug="reseller-a-multi-2"
        )
        reseller_b = baker.make(
            Organization, can_invite_organizations=True, slug="reseller-b-multi-2"
        )

        baker.make(
            OrganizationMembership,
            user=user,
            organization=reseller_a,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=reseller_b,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        client.force_authenticate(user)
        # No X-Organization-Id header.
        client.credentials()

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
class TestBrandingLogoKeyPrefixIsEnforcedOnWrite:
    """BLOCKER 1 (Phase 2b security review): the media bucket is a single shared
    bucket holding `profile_pictures`, `providers_documents`,
    `healthcare_entities_documents` (PHI), and `branding_logos` at their own
    top-level prefixes. A `logo_url` that normalizes to a key outside
    `uploads/branding_logos/` must be rejected on write -- otherwise an eligible
    reseller admin could point their own branding row at another tenant's
    private object and have it served back through the unauthenticated
    delivery route."""

    @pytest.mark.parametrize(
        "logo_url",
        [
            "providers_documents/some-victim-file.pdf",
            "profile_pictures/some-victim-avatar.png",
            "/providers_documents/some-victim-file.pdf",
            "healthcare_entities_documents/some-victim-record.pdf",
            "bare-filename.png",
            "https://example.com/providers_documents/some-victim-file.pdf",
        ],
    )
    def test_foreign_prefix_key_is_rejected(
        self, client, user, reseller_org, reseller_org_admin, logo_url
    ):
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "logo_url": logo_url,
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "support_email": "support@example.com",
            "redirect_url": "https://example.com/return",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
        assert "logo_url" in response.json()
        # The rejected key must never have been persisted.
        assert not OrganizationBranding.objects.filter(organization_id=reseller_org.id).exists()

    def test_empty_logo_url_still_allowed(self, client, user, reseller_org, reseller_org_admin):
        """Clearing the logo (empty string) must stay allowed -- only a non-empty,
        foreign-prefix key is rejected."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        payload = {
            "app_name": "MyScheduler",
            "logo_url": "",
            "primary_color": "#FF0000",
            "secondary_color": "#00FF00",
            "support_email": "support@example.com",
            "redirect_url": "https://example.com/return",
        }

        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_201_CREATED)


@pytest.mark.django_db
class TestBrandingWriteGateAllMethods:
    """The shared write gate (``organizations.permissions.
    evaluate_branding_write_gate``) guards PUT and PATCH on
    ``OrganizationBrandingView`` with its full three-condition check -- Phase 3
    replaces the reseller-only ``_check_reseller_status`` with it (see the
    class docstring on ``OrganizationBrandingView``). GET uses the narrower
    two-condition eligibility gate (``_check_branding_read_gate``): it still
    refuses on parent-present and not-entitled, but -- unlike PUT/PATCH --
    admits a slug-less-but-otherwise-eligible org (see
    ``test_no_slug_eligible_org_get_is_allowed_but_put_and_patch_are_refused``
    below)."""

    def test_eligible_non_reseller_admin_completes_get_put_and_patch(
        self, client, user, eligible_org, eligible_org_admin
    ):
        """The headline behavior change of this phase: a plain, parentless,
        entitled, slugged organization -- not a reseller -- can now manage its
        own branding through every method this endpoint exposes."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        put_payload = {
            "app_name": "EligibleApp",
            "logo_url": "",
            "primary_color": "#112233",
            "secondary_color": "#445566",
            "support_email": "support@example.com",
            "redirect_url": "https://example.com/return",
        }
        put_response = client.put(BRANDING_URL, data=put_payload, format="json")
        assert_response_status_code(put_response, status.HTTP_201_CREATED)
        assert put_response.json()["app_name"] == "EligibleApp"

        get_response = client.get(BRANDING_URL)
        assert_response_status_code(get_response, status.HTTP_200_OK)
        assert get_response.json()["app_name"] == "EligibleApp"

        patch_response = client.patch(
            BRANDING_URL, data={"app_name": "EligibleAppRenamed"}, format="json"
        )
        assert_response_status_code(patch_response, status.HTTP_200_OK)
        assert patch_response.json()["app_name"] == "EligibleAppRenamed"

    def test_admin_of_a_parented_org_gets_403_on_get_put_and_patch(
        self, client, user, parented_org, parented_org_admin
    ):
        """Use-case 5: refused by the backend on every method, not merely
        hidden in the interface -- and nothing is persisted."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(parented_org.id))

        payload = {
            "app_name": "ShouldNotSave",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }
        put_response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(put_response, status.HTTP_403_FORBIDDEN)
        assert "parent" in str(put_response.json()).lower()

        get_response = client.get(BRANDING_URL)
        assert_response_status_code(get_response, status.HTTP_403_FORBIDDEN)

        patch_response = client.patch(BRANDING_URL, data={"app_name": "x"}, format="json")
        assert_response_status_code(patch_response, status.HTTP_403_FORBIDDEN)

        assert not OrganizationBranding.objects.filter(organization=parented_org).exists()

    def test_no_slug_org_gets_403_with_the_pick_a_slug_reason(
        self, client, user, no_slug_org, no_slug_org_admin
    ):
        """Spec edge case: "Eligible org with no public identifier yet" --
        refused with a reason distinct from the other two (named "slug", not
        "parent" or "plan")."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(no_slug_org.id))

        payload = {
            "app_name": "NoSlugApp",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }
        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        assert "slug" in str(response.json()).lower()
        assert not OrganizationBranding.objects.filter(organization=no_slug_org).exists()

    def test_no_slug_eligible_org_get_is_allowed_but_put_and_patch_are_refused(
        self, client, user, no_slug_org, no_slug_org_admin
    ):
        """Capability signal guiding decision (Organization Auth-Area
        Branding plan): a parentless + entitled + slug-less org must still
        SEE the branding page -- GET is admitted by the narrower
        two-condition eligibility gate -- even though writing is refused
        ("pick a slug first") until it does. Matches the spec's "Eligible org
        with no public identifier yet" edge case: settings still offered,
        saving refused. Mirrors the slugged-reseller no-row case
        (``TestResellerNowRequiresASlug.test_reseller_with_a_slug_still_passes_the_gate``):
        404, not 403, when no branding row exists yet; 200 once one does."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(no_slug_org.id))

        # No branding row yet: GET admits the request (the eligibility gate
        # passed) and falls through to the ordinary "no row" 404 -- not 403.
        get_response = client.get(BRANDING_URL)
        assert_response_status_code(get_response, status.HTTP_404_NOT_FOUND)

        # PUT/PATCH still refuse with the "pick a slug first" reason.
        payload = {
            "app_name": "NoSlugApp",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }
        put_response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(put_response, status.HTTP_403_FORBIDDEN)
        assert "slug" in str(put_response.json()).lower()

        patch_response = client.patch(BRANDING_URL, data={"app_name": "x"}, format="json")
        assert_response_status_code(patch_response, status.HTTP_403_FORBIDDEN)
        assert "slug" in str(patch_response.json()).lower()

        # Now with a branding row present, GET succeeds with 200.
        baker.make(
            OrganizationBranding,
            organization=no_slug_org,
            app_name="NoSlugAppExisting",
            logo="",
            primary_color="",
            secondary_color="",
            support_email="",
            redirect_url="",
        )
        get_response_with_row = client.get(BRANDING_URL)
        assert_response_status_code(get_response_with_row, status.HTTP_200_OK)
        assert get_response_with_row.json()["app_name"] == "NoSlugAppExisting"

    def test_non_admin_member_of_an_eligible_org_still_gets_403(
        self, client, eligible_org, eligible_org_member
    ):
        """The write gate widens WHICH organizations are eligible; it does not
        loosen the admin-only requirement within an eligible organization."""
        client.force_authenticate(eligible_org_member)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    @pytest.mark.no_auto_subscription
    def test_free_plan_org_gets_403(self, client, user):
        """Parentless and slugged, but not entitled -- the billing-state refusal."""
        org = _make_unentitled_org(parent=None, slug="free-plan-org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))

        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        detail = str(response.json()).lower()
        assert "plan" in detail or "entitle" in detail

    def test_the_three_refusal_reasons_are_distinguishable(
        self,
        client,
        user,
        parented_org,
        parented_org_admin,
        no_slug_org,
        no_slug_org_admin,
    ):
        """The has-a-parent and no-slug 403 bodies differ -- not a generic
        "forbidden" message both times -- so a caller (or a reviewer reading
        the response) can tell which write-gate condition failed.

        Probed via PUT: the no-slug condition is a write-gate-only refusal
        (GET uses the narrower two-condition eligibility gate and admits a
        slug-less-but-otherwise-eligible org -- see
        ``test_no_slug_eligible_org_get_is_allowed_but_put_and_patch_are_refused``),
        so GET would not reproduce the no-slug 403 body here."""
        client.force_authenticate(user)

        payload = {
            "app_name": "x",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }

        client.credentials(HTTP_X_ORGANIZATION_ID=str(parented_org.id))
        parented_detail = client.put(BRANDING_URL, data=payload, format="json").json()

        client.credentials(HTTP_X_ORGANIZATION_ID=str(no_slug_org.id))
        no_slug_detail = client.put(BRANDING_URL, data=payload, format="json").json()

        assert parented_detail != no_slug_detail
        assert "parent" in str(parented_detail).lower()
        assert "slug" in str(no_slug_detail).lower()
        assert "slug" not in str(parented_detail).lower()
        assert "parent" not in str(no_slug_detail).lower()


@pytest.mark.django_db
class TestResellerNowRequiresASlug:
    """Phase 3 widens the write gate with a THIRD condition (slug-set) on top
    of the pre-existing parentless+entitled pair. A reseller organization is
    parentless in every fixture this suite has, and (via the autouse default
    subscription) entitled by default -- so before this phase a reseller
    always passed the old ``is_reseller()`` gate. It does NOT get a pass on
    the new slug condition: this is a deliberate behavior change for reseller
    fixtures, asserted explicitly here rather than left to be discovered as a
    side effect of ``reseller_org`` now carrying a slug."""

    def test_reseller_without_a_slug_is_now_refused(self, client, user, reseller_org_without_slug):
        """The no-slug refusal is a **write** gate condition (see
        ``OrganizationBrandingView._check_branding_read_gate`` -- GET uses the
        narrower two-condition eligibility gate and does not refuse for a
        missing slug), so probe it via PUT."""
        baker.make(
            OrganizationMembership,
            user=user,
            organization=reseller_org_without_slug,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org_without_slug.id))

        payload = {
            "app_name": "NoSlugApp",
            "logo_url": "",
            "primary_color": "",
            "secondary_color": "",
            "support_email": "",
            "redirect_url": "",
        }
        response = client.put(BRANDING_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)
        assert "slug" in str(response.json()).lower()

    def test_reseller_with_a_slug_still_passes_the_gate(
        self, client, user, reseller_org, reseller_org_admin
    ):
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(reseller_org.id))

        # No branding configured yet: 404 (not 403) proves the gate itself
        # admitted the request -- IsOrganizationAdmin + the write gate both
        # passed, and the endpoint fell through to the "no row yet" branch.
        response = client.get(BRANDING_URL)
        assert_response_status_code(response, status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
class TestCanManageBrandingOnMembershipPayload:
    """``can_manage_branding`` on the membership payloads the SPA already
    fetches (``GET /organizations/current/`` and ``GET /organizations/mine/``)
    -- Organization Auth-Area Branding plan, Phase 4 Capability signal.

    Covers all four write-gate cases named in the phase spec, plus the
    no-slug-but-eligible case that is the whole point of the field (it must
    read ``True`` there, unlike the write gate itself)."""

    def _current_url(self):
        return reverse("api:Organizations-current")

    def _mine_url(self):
        return reverse("api:Organizations-mine")

    def test_eligible_org_reports_true(self, client, user, eligible_org, eligible_org_admin):
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        current_response = client.get(self._current_url())
        assert_response_status_code(current_response, status.HTTP_200_OK)
        assert current_response.json()["can_manage_branding"] is True

        mine_response = client.get(self._mine_url())
        assert_response_status_code(mine_response, status.HTTP_200_OK)
        [entry] = mine_response.json()
        assert entry["can_manage_branding"] is True

    def test_eligible_org_with_no_slug_still_reports_true(self, client, user, no_slug_org):
        """The key case: an org one write-gate step away (missing only a slug)
        must still report `can_manage_branding=True` -- see the plan's
        Capability signal guiding decision. Distinguishes this field from the
        write gate, which would refuse this exact organization."""
        baker.make(
            OrganizationMembership,
            user=user,
            organization=no_slug_org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(no_slug_org.id))

        current_response = client.get(self._current_url())
        assert_response_status_code(current_response, status.HTTP_200_OK)
        assert current_response.json()["can_manage_branding"] is True

    def test_parented_org_reports_false(self, client, user, parented_org):
        baker.make(
            OrganizationMembership,
            user=user,
            organization=parented_org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(parented_org.id))

        current_response = client.get(self._current_url())
        assert_response_status_code(current_response, status.HTTP_200_OK)
        assert current_response.json()["can_manage_branding"] is False

        mine_response = client.get(self._mine_url())
        assert_response_status_code(mine_response, status.HTTP_200_OK)
        [entry] = mine_response.json()
        assert entry["can_manage_branding"] is False

    @pytest.mark.no_auto_subscription
    def test_free_plan_org_reports_false(self, client, user):
        free_org = _make_unentitled_org(parent=None, slug="free-cap-org")
        baker.make(
            OrganizationMembership,
            user=user,
            organization=free_org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(free_org.id))

        current_response = client.get(self._current_url())
        assert_response_status_code(current_response, status.HTTP_200_OK)
        assert current_response.json()["can_manage_branding"] is False

        mine_response = client.get(self._mine_url())
        assert_response_status_code(mine_response, status.HTTP_200_OK)
        [entry] = mine_response.json()
        assert entry["can_manage_branding"] is False

    def test_non_admin_member_of_an_eligible_org_still_reports_true(
        self, client, eligible_org, eligible_org_member
    ):
        """A non-admin member's payload reports the same organization-level
        capability as an admin's -- `can_manage_branding` is not gated by the
        caller's own role, only by the organization's eligibility. Write
        authorization stays role-gated separately, on the branding endpoints
        themselves (`IsOrganizationAdmin`)."""
        client.force_authenticate(eligible_org_member)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        current_response = client.get(self._current_url())
        assert_response_status_code(current_response, status.HTTP_200_OK)
        assert current_response.json()["can_manage_branding"] is True

    def test_mine_endpoint_entitlement_queries_do_not_scale_with_membership_count(self):
        """Reviewer finding: the N+1-shaped entitlement lookup on
        ``GET /organizations/mine/`` -- ``MyMembershipSerializer.get_can_manage_branding``
        used to call ``is_branding_eligible_organization`` once per membership
        row, so the number of subscription/entitlement queries scaled linearly
        with the caller's distinct-org membership count. Batched via
        ``is_branding_eligible_organizations`` /
        ``EntitlementService.has_entitlement_for_organizations``: the number of
        ``payments_subscription`` queries the endpoint issues must stay the
        same regardless of how many organizations the caller belongs to."""

        def _subscription_query_count(organization_count: int) -> int:
            caller = baker.make(User)
            for index in range(organization_count):
                org = baker.make(
                    Organization,
                    can_invite_organizations=False,
                    slug=f"batch-org-{organization_count}-{index}",
                )
                baker.make(
                    OrganizationMembership,
                    user=caller,
                    organization=org,
                    role=OrganizationRole.ADMIN,
                    is_active=True,
                )
            local_client = APIClient()
            local_client.force_authenticate(caller)
            with CaptureQueriesContext(connection) as captured:
                response = local_client.get(self._mine_url())
            assert_response_status_code(response, status.HTTP_200_OK)
            assert len(response.json()) == organization_count
            return sum(
                1 for query in captured.captured_queries if "payments_subscription" in query["sql"]
            )

        small_batch_query_count = _subscription_query_count(2)
        large_batch_query_count = _subscription_query_count(5)
        assert small_batch_query_count == large_batch_query_count
