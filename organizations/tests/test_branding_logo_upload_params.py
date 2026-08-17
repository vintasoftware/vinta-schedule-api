import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient
from s3direct.utils import AWSCredentials

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.tests.helpers import make_membership
from organizations.tests.test_branding_rest import _make_unentitled_org


User = get_user_model()

UPLOAD_PARAMS_URL = "/branding/logo-upload-params/"

S3_TEST_SETTINGS = {
    "AWS_STORAGE_BUCKET_NAME": "test-bucket",
    "AWS_S3_REGION_NAME": "us-east-1",
    "AWS_S3_ENDPOINT_URL": "https://s3.us-east-1.amazonaws.com",
}


def assert_response_status_code(response, expected_status_code):
    assert response.status_code == expected_status_code, (
        f"The status error {response.status_code} != {expected_status_code}\n"
        f"Response Payload: {json.dumps(response.json() if hasattr(response, 'json') and callable(response.json) else str(response.content))}"
    )


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def user():
    return baker.make(User)


@pytest.fixture
def eligible_org():
    """Parentless, entitled (suite's autouse default subscription), slugged org."""
    return baker.make(Organization, can_invite_organizations=False, slug="eligible-org")


@pytest.fixture
def second_eligible_org():
    """A second parentless, entitled organization.

    This used to be a ``no_slug_org``: parentless and entitled but deliberately
    slug-less, pinning that logo upload goes through the eligibility gate rather
    than the (then three-condition) write gate. ``Organization.slug`` is NOT
    NULL now, so that state is unreachable -- what the fixture still buys is a
    second eligible organization that is not the one every other test uses."""
    return baker.make(Organization, can_invite_organizations=False, parent=None)


@pytest.fixture
def parented_org(eligible_org):
    return baker.make(Organization, parent=eligible_org, slug="child-org")


@pytest.fixture
def eligible_org_admin(user, eligible_org):
    return make_membership(
        user=user,
        organization=eligible_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def eligible_org_member(eligible_org):
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
def second_eligible_org_admin(user, second_eligible_org):
    return make_membership(
        user=user,
        organization=second_eligible_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def parented_org_admin(user, parented_org):
    return make_membership(
        user=user,
        organization=parented_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


VALID_PAYLOAD = {"file_name": "logo.png", "file_type": "image/png", "file_size": 1024}


@pytest.fixture(autouse=True)
def _s3_upload_settings(settings):
    """Every test in this module posts to the signing endpoint, which now needs a
    complete S3 config (bucket/region/endpoint) to mint a presigned URL -- these
    settings aren't defined in `vinta_schedule_api.settings.test`. Credentials are
    mocked too, since `generate_presigned_url` needs a structurally valid access
    key/secret to sign with (it never makes a network call)."""
    for k, v in S3_TEST_SETTINGS.items():
        setattr(settings, k, v)
    with patch(
        "organizations.branding_logo.get_aws_credentials",
        return_value=AWSCredentials(token=None, secret_key="secret", access_key="AKIATEST"),
    ):
        yield


@pytest.mark.django_db
class TestOrganizationBrandingLogoUploadParamsView:
    def test_admin_of_eligible_org_gets_signed_params(
        self, client, user, eligible_org, eligible_org_admin
    ):
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert data["object_key"]
        assert data["upload_url"].startswith("https://")
        assert data["expires_in"] == 900

    def test_returns_a_presigned_url_the_browser_can_put_to_unaided(
        self, client, user, eligible_org, eligible_org_admin
    ):
        """The response must carry a complete SigV4 presigned URL: the SPA
        authenticates with JWT and has no way to reach s3direct's session+CSRF
        signing view, so it cannot sign a request itself. Everything S3 needs has
        to be in this URL's query string or the upload PUT goes out unsigned and
        S3 answers 403."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        parsed = urlparse(data["upload_url"])
        query = parse_qs(parsed.query)

        assert parsed.path.endswith(data["object_key"])
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-Signature"][0]
        assert query["X-Amz-Credential"][0].startswith("AKIATEST/")
        assert int(query["X-Amz-Expires"][0]) == data["expires_in"]

    def test_presigned_url_signs_content_type_but_never_an_acl(
        self, client, user, eligible_org, eligible_org_admin
    ):
        """No `x-amz-acl` anywhere: the media bucket has ACLs disabled
        (BucketOwnerEnforced). Content-Type is signed so the type is fixed when
        the URL is issued and cannot be swapped on the PUT."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        upload_url = response.json()["upload_url"]
        signed_headers = parse_qs(urlparse(upload_url).query)["X-Amz-SignedHeaders"][0]

        assert "x-amz-acl" not in signed_headers
        assert "acl" not in upload_url.lower()
        assert "content-type" in signed_headers

    def test_response_no_longer_leaks_aws_credentials_to_the_browser(
        self, client, user, eligible_org, eligible_org_admin
    ):
        """The presigned URL replaces the raw key id the old handshake had to
        expose."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

        data = response.json()
        assert "access_key_id" not in data
        assert "session_token" not in data

    def test_admin_of_any_eligible_org_gets_signed_params(
        self, client, user, second_eligible_org, second_eligible_org_admin
    ):
        """Logo upload goes through the eligibility gate (parentless and
        entitled), not the branding write gate -- uploads happen on file-picker
        change, before the branding write on form submit."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(second_eligible_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_200_OK)

    def test_non_admin_member_returns_403(self, client, eligible_org, eligible_org_member):
        client.force_authenticate(eligible_org_member)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_admin_of_a_parented_org_returns_403(
        self, client, user, parented_org, parented_org_admin
    ):
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(parented_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    @pytest.mark.no_auto_subscription
    def test_admin_of_an_unentitled_org_returns_403(self, client, user):
        org = _make_unentitled_org(can_invite_organizations=False, slug="unentitled-org")
        make_membership(
            user=user,
            organization=org,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_403_FORBIDDEN)

    def test_unauthenticated_returns_401(self, client, eligible_org):
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_401_UNAUTHORIZED)

    def test_disallowed_content_type_returns_400(
        self, client, user, eligible_org, eligible_org_admin
    ):
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        payload = {**VALID_PAYLOAD, "file_type": "image/svg+xml"}
        response = client.post(UPLOAD_PARAMS_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_oversized_file_returns_400(self, client, user, eligible_org, eligible_org_admin):
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(eligible_org.id))

        payload = {
            **VALID_PAYLOAD,
            "file_size": settings.BRANDING_LOGO_MAX_SIZE_BYTES + 1,
        }
        response = client.post(UPLOAD_PARAMS_URL, data=payload, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)

    def test_multi_org_admin_without_header_returns_400(self, client):
        """Proves TenantScopedViewMixin is actually wired: a multi-org admin
        omitting X-Organization-Id gets 400, not a silent fallback to the
        oldest membership."""
        user = baker.make(User)
        org_a = baker.make(Organization, can_invite_organizations=False, slug="multi-a")
        org_b = baker.make(Organization, can_invite_organizations=False, slug="multi-b")
        make_membership(
            user=user,
            organization=org_a,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        make_membership(
            user=user,
            organization=org_b,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        client.force_authenticate(user)
        client.credentials()

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
