import pytest
from model_bakery import baker

from calendar_integration.models import Calendar, CalendarOwnership
from organizations.models import Organization, OrganizationMembership
from public_api.models import SystemUser
from public_api.scoping import scoped_calendar_ids
from public_api.services import PublicAPIAuthService
from users.models import User


@pytest.mark.django_db
class TestScopedCalendarIds:
    """Test suite for the scoped_calendar_ids helper function."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization, name="Test Organization")

    @pytest.fixture
    def another_organization(self):
        """Create another test organization."""
        return baker.make(Organization, name="Another Organization")

    @pytest.fixture
    def user(self, organization):
        """Create a test user in the organization."""
        return baker.make(User, email="user@example.com")

    @pytest.fixture
    def another_user(self, organization):
        """Create another test user in the organization."""
        return baker.make(User, email="another@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create an active membership for the main user in the main organization."""
        return baker.make(OrganizationMembership, user=user, organization=organization, is_active=True)

    @pytest.fixture
    def another_membership(self, organization, another_user):
        """Create an active membership for another user in the main organization."""
        return baker.make(
            OrganizationMembership, user=another_user, organization=organization, is_active=True
        )

    @pytest.fixture
    def calendar_owned_by_user(self, organization, user):
        """Create a calendar owned by the user."""
        calendar = baker.make(
            Calendar, organization=organization, name="User Calendar", external_id="user-cal"
        )
        baker.make(CalendarOwnership, calendar=calendar, user=user, organization=organization)
        return calendar

    @pytest.fixture
    def calendar_owned_by_another_user(self, organization, another_user):
        """Create a calendar owned by another user."""
        calendar = baker.make(
            Calendar,
            organization=organization,
            name="Another User Calendar",
            external_id="another-user-cal",
        )
        baker.make(
            CalendarOwnership, calendar=calendar, user=another_user, organization=organization
        )
        return calendar

    @pytest.fixture
    def calendar_in_another_org(self, another_organization, user):
        """Create a calendar in another organization owned by the same user id."""
        calendar = baker.make(
            Calendar,
            organization=another_organization,
            name="Other Org Calendar",
            external_id="other-org-cal",
        )
        baker.make(
            CalendarOwnership, calendar=calendar, user=user, organization=another_organization
        )
        return calendar

    @pytest.fixture
    def org_wide_system_user(self, organization):
        """Create an organization-wide system user (no owner scope)."""
        auth_service = PublicAPIAuthService()
        system_user, _ = auth_service.create_system_user(
            integration_name="org_wide_token", organization=organization
        )
        return system_user

    @pytest.fixture
    def scoped_system_user(self, organization, membership):
        """Create a system user scoped to a specific membership."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            scoped_to_membership_fk=membership,
            integration_name="scoped_token",
        )
        return system_user

    @pytest.fixture
    def scoped_system_user_no_calendars(self, organization, another_membership):
        """Create a system user scoped to a membership whose user owns no calendars."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            scoped_to_membership_fk=another_membership,
            integration_name="scoped_token_no_calendars",
        )
        return system_user

    def test_returns_none_for_org_wide_token(self, org_wide_system_user, organization):
        """Test that org-wide tokens (scoped_to_membership IS NULL) return None."""
        result = scoped_calendar_ids(org_wide_system_user, organization)
        assert result is None

    def test_returns_owners_calendar_ids_for_scoped_token(
        self, scoped_system_user, organization, calendar_owned_by_user
    ):
        """Test that scoped tokens return only the membership user's calendar IDs."""
        result = scoped_calendar_ids(scoped_system_user, organization)
        assert result is not None
        assert calendar_owned_by_user.id in result
        assert len(result) == 1

    def test_returns_empty_set_for_owner_with_no_calendars(
        self, scoped_system_user_no_calendars, organization
    ):
        """Test that scoped tokens for memberships whose user owns no calendars return an empty set."""
        result = scoped_calendar_ids(scoped_system_user_no_calendars, organization)
        assert result is not None
        assert isinstance(result, set)
        assert len(result) == 0

    def test_respects_organization_filter(
        self,
        scoped_system_user,
        organization,
        calendar_owned_by_user,
        calendar_in_another_org,
    ):
        """Test that the helper never returns calendars from other organizations."""
        result = scoped_calendar_ids(scoped_system_user, organization)
        assert result is not None
        # Should only include the calendar from the queried organization
        assert calendar_owned_by_user.id in result
        # Should NOT include the calendar from another organization, even though the same user owns it
        assert calendar_in_another_org.id not in result

    def test_excludes_calendars_owned_by_other_users(
        self,
        scoped_system_user,
        organization,
        calendar_owned_by_user,
        calendar_owned_by_another_user,
    ):
        """Test that the helper only returns calendars owned by the membership's user."""
        result = scoped_calendar_ids(scoped_system_user, organization)
        assert result is not None
        # Should include calendars owned by the membership's user
        assert calendar_owned_by_user.id in result
        # Should not include calendars owned by other users
        assert calendar_owned_by_another_user.id not in result

    def test_multiple_calendars_owned_by_scoped_user(
        self, scoped_system_user, organization, user
    ):
        """Test that the helper returns all calendars owned by the membership's user."""
        calendar1 = baker.make(
            Calendar, organization=organization, name="Calendar 1", external_id="test-cal-1"
        )
        calendar2 = baker.make(
            Calendar, organization=organization, name="Calendar 2", external_id="test-cal-2"
        )
        baker.make(CalendarOwnership, calendar=calendar1, user=user, organization=organization)
        baker.make(CalendarOwnership, calendar=calendar2, user=user, organization=organization)

        result = scoped_calendar_ids(scoped_system_user, organization)
        assert result is not None
        assert calendar1.id in result
        assert calendar2.id in result
        assert len(result) == 2

    def test_scoped_membership_resolves_when_org_matches(
        self, organization, user, membership
    ):
        """Test that scoped_to_membership resolves correctly when SystemUser.organization matches the membership's org."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            scoped_to_membership_fk=membership,
            integration_name="scoped_token_org_check",
        )
        # SystemUser.organization must match the membership's organization for the
        # OrganizationForeignKey tenant join to resolve correctly.
        assert system_user.organization_id == membership.organization_id
        # The scalar id is always accessible regardless of the FK join path.
        assert system_user.scoped_to_membership_fk_id == membership.id
