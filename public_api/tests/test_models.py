"""Tests for ``public_api.models``."""

import pytest
from model_bakery import baker

from organizations.models import Organization
from organizations.permission_catalog import GROUP_ORGANIZATION_ADMIN, GROUP_ORGANIZATION_MEMBER
from organizations.tests.helpers import make_membership
from public_api.constants import PublicAPIResources
from public_api.models import ResourceAccess, SystemUser
from users.models import User


@pytest.mark.django_db
class TestSystemUserCanBookAnyAppointmentType:
    """Only a token of the organization holding the ``appointment_type`` resource, and
    either organization-wide or scoped to an admin member, may book any of its
    appointment types."""

    @pytest.fixture
    def organization(self):
        return baker.make(Organization, name="Booking Organization")

    def _token(self, organization, *, resources=(PublicAPIResources.APPOINTMENT_TYPE,), **kwargs):
        system_user = baker.make(SystemUser, organization=organization, **kwargs)
        for resource in resources:
            baker.make(ResourceAccess, system_user=system_user, resource_name=resource)
        return system_user

    def test_org_wide_token_with_the_resource_may_book(self, organization):
        assert self._token(organization).can_book_any_appointment_type(organization.id) is True

    def test_org_wide_token_without_the_resource_may_not_book(self, organization):
        """The resource is checked here, not left to the caller's permission class."""
        system_user = self._token(organization, resources=(PublicAPIResources.CALENDAR_EVENT,))

        assert system_user.can_book_any_appointment_type(organization.id) is False

    @pytest.mark.parametrize(
        ("groups", "may_book"),
        [((GROUP_ORGANIZATION_ADMIN,), True), ((GROUP_ORGANIZATION_MEMBER,), False)],
        ids=["scoped-admin", "scoped-member"],
    )
    def test_member_scoped_token_may_book_only_as_an_admin(self, organization, groups, may_book):
        """A token scoped to an admin member reaches every appointment type, as it does
        everywhere else in the public API (``system_user_scope``); one scoped to a
        regular member does not."""
        membership = make_membership(
            user=baker.make(User, email="scoped-booker@example.com"),
            organization=organization,
            groups=groups,
            is_active=True,
        )
        system_user = self._token(organization, scoped_to_membership_user_id=membership.user_id)

        assert system_user.can_book_any_appointment_type(organization.id) is may_book

    def test_token_scoped_to_an_inactive_admin_may_not_book(self, organization):
        """A scoped token whose admin membership was deactivated fails closed."""
        membership = make_membership(
            user=baker.make(User, email="former-admin@example.com"),
            organization=organization,
            groups=(GROUP_ORGANIZATION_ADMIN,),
            is_active=False,
        )
        system_user = self._token(organization, scoped_to_membership_user_id=membership.user_id)

        assert system_user.can_book_any_appointment_type(organization.id) is False

    @pytest.mark.parametrize("token_organization", ["other", "none"])
    def test_token_of_no_or_other_organization_may_not_book(self, organization, token_organization):
        """A token with no organization, or of another one, could name any organization
        in the request header."""
        other = baker.make(Organization, name="Other Organization")
        system_user = self._token(other if token_organization == "other" else None)

        assert system_user.can_book_any_appointment_type(organization.id) is False
