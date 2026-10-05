import pytest
from model_bakery import baker

from common.feature_flags import (
    RESOURCE_CALENDAR_PROVIDER_SYNC,
    is_enabled,
    organization_ids_with_flag,
)
from common.organization_context import organization_context
from organizations.models import Organization, OrganizationFeatureFlag


def _set_flag(organization: Organization, key: str, enabled: bool) -> None:
    with organization_context(organization):
        OrganizationFeatureFlag.objects.create(organization=organization, key=key, enabled=enabled)


@pytest.mark.django_db
class TestIsEnabled:
    def test_missing_row_means_off(self):
        organization = baker.make(Organization)

        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id) is False

    def test_enabled_row_means_on(self):
        organization = baker.make(Organization)
        _set_flag(organization, RESOURCE_CALENDAR_PROVIDER_SYNC, True)

        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id) is True

    def test_disabled_row_means_off(self):
        organization = baker.make(Organization)
        _set_flag(organization, RESOURCE_CALENDAR_PROVIDER_SYNC, False)

        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id) is False

    def test_other_key_does_not_enable_the_flag(self):
        organization = baker.make(Organization)
        _set_flag(organization, "some_other_flag", True)

        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, organization.id) is False

    def test_another_organizations_row_does_not_leak(self):
        enabled_org = baker.make(Organization)
        other_org = baker.make(Organization)
        _set_flag(enabled_org, RESOURCE_CALENDAR_PROVIDER_SYNC, True)

        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, enabled_org.id) is True
        assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, other_org.id) is False

    def test_works_with_another_organization_bound(self):
        enabled_org = baker.make(Organization)
        other_org = baker.make(Organization)
        _set_flag(enabled_org, RESOURCE_CALENDAR_PROVIDER_SYNC, True)

        with organization_context(other_org):
            assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, enabled_org.id) is True
            assert is_enabled(RESOURCE_CALENDAR_PROVIDER_SYNC, other_org.id) is False


@pytest.mark.django_db
class TestOrganizationIdsWithFlag:
    def test_returns_only_enabled_organizations_for_the_key(self):
        on_a = baker.make(Organization)
        on_b = baker.make(Organization)
        off = baker.make(Organization)
        other_key = baker.make(Organization)
        baker.make(Organization)  # no row at all
        _set_flag(on_a, RESOURCE_CALENDAR_PROVIDER_SYNC, True)
        _set_flag(on_b, RESOURCE_CALENDAR_PROVIDER_SYNC, True)
        _set_flag(off, RESOURCE_CALENDAR_PROVIDER_SYNC, False)
        _set_flag(other_key, "some_other_flag", True)

        assert organization_ids_with_flag(RESOURCE_CALENDAR_PROVIDER_SYNC) == sorted(
            [on_a.id, on_b.id]
        )

    def test_returns_empty_list_when_nothing_enabled(self):
        assert organization_ids_with_flag(RESOURCE_CALENDAR_PROVIDER_SYNC) == []
