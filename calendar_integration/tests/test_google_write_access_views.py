"""Tests for ``POST /calendar/google-service-account/verify-write-access/``."""

import uuid
from collections.abc import Iterator
from unittest.mock import Mock, patch

from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status

from calendar_integration.models import GoogleCalendarServiceAccount
from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC
from common.redis import ResilientLimiter
from organizations.models import Organization, OrganizationFeatureFlag, OrganizationMembership
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN
from organizations.tests.helpers import grant_membership_groups


ADAPTER_MODULE = "calendar_integration.services.calendar_adapters.google_calendar_adapter"
URL_NAME = "api:GoogleServiceAccountWriteAccess-verify-write-access"


@pytest.fixture
def google_directory() -> Iterator[Mock]:
    """Patch Google's credential builder and ``build``; yield the admin client."""
    admin_client = Mock()
    with (
        patch(f"{ADAPTER_MODULE}.google_service_account.Credentials.from_service_account_info"),
        patch(
            f"{ADAPTER_MODULE}.build",
            side_effect=lambda service, *_, **__: admin_client if service == "admin" else Mock(),
        ),
        patch(f"{ADAPTER_MODULE}.read_quote_limiter", spec=ResilientLimiter),
        patch(f"{ADAPTER_MODULE}.write_quote_limiter", spec=ResilientLimiter),
    ):
        yield admin_client


@pytest.fixture
def organization(user):
    org = baker.make(Organization, name=f"Org {uuid.uuid4().hex[:6]}")
    baker.make(OrganizationMembership, user=user, organization=org)
    return org


@pytest.fixture
def admin_user(user, organization):
    membership = OrganizationMembership.objects.get(user=user, organization=organization)
    grant_membership_groups(membership, [GROUP_ORGANIZATION_ADMIN])
    return user


@pytest.fixture
def flag_on(organization):
    return OrganizationFeatureFlag.objects.create(
        organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
    )


@pytest.fixture
def service_account(organization):
    return GoogleCalendarServiceAccount.objects.create(
        organization=organization,
        email="service@example.com",
        admin_email="admin@example.com",
        private_key_id="key-id",
        private_key="private-key",
    )


def _post(client, organization):
    return client.post(reverse(URL_NAME), HTTP_X_ORGANIZATION_ID=str(organization.id))


@pytest.mark.django_db
class TestVerifyWriteAccessView:
    def test_url(self):
        assert reverse(URL_NAME) == "/calendar/google-service-account/verify-write-access/"

    def test_flag_on_with_granted_scope_sets_write_enabled(
        self, auth_client, admin_user, organization, flag_on, service_account, google_directory
    ):
        response = _post(auth_client, organization)

        assert response.status_code == status.HTTP_200_OK, response.content
        service_account.refresh_from_db()
        assert service_account.write_enabled is True
        body = response.json()
        assert body["write_enabled"] is True
        assert body["error"] == ""
        assert body["write_verified_at"] is not None

    def test_flag_off_returns_404(
        self, auth_client, admin_user, organization, service_account, google_directory
    ):
        response = _post(auth_client, organization)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        service_account.refresh_from_db()
        assert service_account.write_enabled is False
        google_directory.resources.assert_not_called()

    def test_flag_disabled_row_returns_404(
        self, auth_client, admin_user, organization, service_account, google_directory
    ):
        OrganizationFeatureFlag.objects.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=False
        )

        response = _post(auth_client, organization)

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_non_admin_is_forbidden(
        self, auth_client, organization, flag_on, service_account, google_directory
    ):
        response = _post(auth_client, organization)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        service_account.refresh_from_db()
        assert service_account.write_enabled is False
        google_directory.resources.assert_not_called()

    def test_anonymous_is_rejected(self, anonymous_client, organization, flag_on):
        response = _post(anonymous_client, organization)

        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_of_another_org_cannot_verify_this_one(
        self, auth_client, admin_user, flag_on, service_account, google_directory
    ):
        other = baker.make(Organization)
        OrganizationFeatureFlag.objects.create(
            organization=other, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
        )

        response = _post(auth_client, other)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        service_account.refresh_from_db()
        assert service_account.write_enabled is False

    def test_get_is_not_allowed(self, auth_client, admin_user, organization, flag_on):
        response = auth_client.get(reverse(URL_NAME), HTTP_X_ORGANIZATION_ID=str(organization.id))

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED
