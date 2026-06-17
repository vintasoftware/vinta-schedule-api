"""Tests for the reseller capability gate."""

import pytest
from model_bakery import baker
from rest_framework.exceptions import PermissionDenied

from organizations.models import Organization
from public_api.capabilities import assert_org_can_invite


@pytest.mark.django_db
class TestAssertOrgCanInvite:
    """Unit tests for assert_org_can_invite gate."""

    def test_assert_org_can_invite_passes_when_flag_true(self):
        """assert_org_can_invite raises nothing when can_invite_organizations=True."""
        org = baker.make(Organization, can_invite_organizations=True)
        # Should not raise
        assert_org_can_invite(org)

    def test_assert_org_can_invite_raises_when_flag_false(self):
        """assert_org_can_invite raises PermissionDenied when can_invite_organizations=False."""
        org = baker.make(Organization, can_invite_organizations=False)
        with pytest.raises(PermissionDenied):
            assert_org_can_invite(org)

    def test_assert_org_can_invite_raises_with_meaningful_message(self):
        """The raised PermissionDenied carries a clear message."""
        org = baker.make(Organization, can_invite_organizations=False)
        with pytest.raises(PermissionDenied) as exc_info:
            assert_org_can_invite(org)
        assert "permission to invite" in str(exc_info.value.detail).lower()
