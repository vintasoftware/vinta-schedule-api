from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

import pytest
from model_bakery import baker

from common.feature_flags import RESOURCE_CALENDAR_PROVIDER_SYNC, is_enabled
from organizations.models import Organization, OrganizationFeatureFlag


User = get_user_model()


@pytest.fixture
def admin_client():
    superuser = User.objects.create_superuser(
        email="flag-admin@example.com",
        password="adminpassword",  # noqa: S106
    )
    client = Client()
    client.force_login(superuser)
    return client


@pytest.mark.django_db
class TestOrganizationFeatureFlagAdmin:
    def test_changelist_renders_rows_from_every_organization_and_filters(self, admin_client):
        org_a = baker.make(Organization, name="Flag Org A")
        org_b = baker.make(Organization, name="Flag Org B")
        OrganizationFeatureFlag.original_manager.create(
            organization=org_a, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
        )
        OrganizationFeatureFlag.original_manager.create(
            organization=org_b, key="other_flag", enabled=False
        )

        response = admin_client.get(
            reverse("admin:organizations_organizationfeatureflag_changelist")
        )

        assert response.status_code == 200
        content = response.content.decode()
        assert RESOURCE_CALENDAR_PROVIDER_SYNC in content
        assert "other_flag" in content
        assert "?key=" in content
        assert "?enabled__exact=1" in content

    def test_change_form_renders(self, admin_client):
        organization = baker.make(Organization)
        flag = OrganizationFeatureFlag.original_manager.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=False
        )

        response = admin_client.get(
            reverse("admin:organizations_organizationfeatureflag_change", args=[flag.pk])
        )

        assert response.status_code == 200

    def test_add_form_saves_a_row_that_enables_the_flag(self, admin_client):
        organization = baker.make(Organization)

        response = admin_client.post(
            reverse("admin:organizations_organizationfeatureflag_add"),
            data={
                "organization": organization.pk,
                "key": RESOURCE_CALENDAR_PROVIDER_SYNC,
                "enabled": "on",
            },
        )

        assert response.status_code == 302
        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id) is True

    def test_change_form_toggles_the_flag_off(self, admin_client):
        organization = baker.make(Organization)
        flag = OrganizationFeatureFlag.original_manager.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=True
        )

        response = admin_client.post(
            reverse("admin:organizations_organizationfeatureflag_change", args=[flag.pk]),
            data={
                "organization": organization.pk,
                "key": RESOURCE_CALENDAR_PROVIDER_SYNC,
            },
        )

        assert response.status_code == 302
        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id) is False

    def test_add_form_rejects_a_duplicate_key_for_the_same_organization(self, admin_client):
        organization = baker.make(Organization)
        OrganizationFeatureFlag.original_manager.create(
            organization=organization, key=RESOURCE_CALENDAR_PROVIDER_SYNC, enabled=False
        )

        response = admin_client.post(
            reverse("admin:organizations_organizationfeatureflag_add"),
            data={
                "organization": organization.pk,
                "key": RESOURCE_CALENDAR_PROVIDER_SYNC,
                "enabled": "on",
            },
        )

        assert response.status_code == 200
        assert (
            OrganizationFeatureFlag.original_manager.filter(organization=organization).count() == 1
        )
