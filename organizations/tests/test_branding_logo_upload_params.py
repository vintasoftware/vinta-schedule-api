import json

from django.conf import settings
from django.contrib.auth import get_user_model

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership, OrganizationRole
from organizations.tests.test_branding_rest import _make_unentitled_org


User = get_user_model()

UPLOAD_PARAMS_URL = "/branding/logo-upload-params/"


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
def no_slug_org():
    """Parentless and entitled, but no slug -- logo upload must still be admitted
    (uploads happen before the slug/branding write on form submit)."""
    return baker.make(Organization, can_invite_organizations=False, parent=None)


@pytest.fixture
def parented_org(eligible_org):
    return baker.make(Organization, parent=eligible_org, slug="child-org")


@pytest.fixture
def eligible_org_admin(user, eligible_org):
    return baker.make(
        OrganizationMembership,
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
def no_slug_org_admin(user, no_slug_org):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=no_slug_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


@pytest.fixture
def parented_org_admin(user, parented_org):
    return baker.make(
        OrganizationMembership,
        user=user,
        organization=parented_org,
        role=OrganizationRole.ADMIN,
        is_active=True,
    )


VALID_PAYLOAD = {"file_name": "logo.png", "file_type": "image/png", "file_size": 1024}


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
        assert data["acl"]

    def test_admin_of_a_no_slug_org_still_gets_signed_params(
        self, client, user, no_slug_org, no_slug_org_admin
    ):
        """Logo upload uses the two-condition eligibility gate, not the
        three-condition write gate -- a slug-less-but-otherwise-eligible org
        must still be able to upload a logo (the slug/branding write happens
        later, on form submit)."""
        client.force_authenticate(user)
        client.credentials(HTTP_X_ORGANIZATION_ID=str(no_slug_org.id))

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
        baker.make(
            OrganizationMembership,
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
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org_a,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org_b,
            role=OrganizationRole.ADMIN,
            is_active=True,
        )

        client.force_authenticate(user)
        client.credentials()

        response = client.post(UPLOAD_PARAMS_URL, data=VALID_PAYLOAD, format="json")
        assert_response_status_code(response, status.HTTP_400_BAD_REQUEST)
