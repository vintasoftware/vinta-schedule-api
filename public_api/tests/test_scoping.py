import pytest
from model_bakery import baker

from calendar_integration.models import Calendar, CalendarOwnership
from organizations.models import Organization, OrganizationMembership
from public_api.models import SystemUser
from public_api.scoping import assert_calendar_in_owner_scope, scoped_calendar_ids
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
        return baker.make(
            OrganizationMembership, user=user, organization=organization, is_active=True
        )

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
        OrganizationMembership.objects.get_or_create(user=user, organization=organization)
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=user.id,
            organization=organization,
        )
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
        OrganizationMembership.objects.get_or_create(user=another_user, organization=organization)
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=another_user.id,
            organization=organization,
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
        OrganizationMembership.objects.get_or_create(user=user, organization=another_organization)
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=user.id,
            organization=another_organization,
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
            scoped_to_membership_user_id=membership.user_id,
            integration_name="scoped_token",
        )
        return system_user

    @pytest.fixture
    def scoped_system_user_no_calendars(self, organization, another_membership):
        """Create a system user scoped to a membership whose user owns no calendars."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            scoped_to_membership_user_id=another_membership.user_id,
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

    def test_multiple_calendars_owned_by_scoped_user(self, scoped_system_user, organization, user):
        """Test that the helper returns all calendars owned by the membership's user."""
        calendar1 = baker.make(
            Calendar, organization=organization, name="Calendar 1", external_id="test-cal-1"
        )
        calendar2 = baker.make(
            Calendar, organization=organization, name="Calendar 2", external_id="test-cal-2"
        )
        OrganizationMembership.objects.get_or_create(user=user, organization=organization)
        baker.make(
            CalendarOwnership,
            calendar=calendar1,
            membership_user_id=user.id,
            organization=organization,
        )
        baker.make(
            CalendarOwnership,
            calendar=calendar2,
            membership_user_id=user.id,
            organization=organization,
        )

        result = scoped_calendar_ids(scoped_system_user, organization)
        assert result is not None
        assert calendar1.id in result
        assert calendar2.id in result
        assert len(result) == 2

    def test_scoped_membership_resolves_when_org_matches(self, organization, user, membership):
        """Test that scoped_to_membership resolves correctly when SystemUser.organization matches the membership's org."""
        system_user = baker.make(
            SystemUser,
            organization=organization,
            scoped_to_membership_user_id=membership.user_id,
            integration_name="scoped_token_org_check",
        )
        # SystemUser.organization must match the membership's organization for the
        # (organization_id, user_id) membership join to resolve correctly.
        assert system_user.organization_id == membership.organization_id
        # The denormalized membership user_id is always accessible on the row.
        assert system_user.scoped_to_membership_user_id == membership.user_id


@pytest.mark.django_db
class TestAssertCalendarInOwnerScope:
    """Unit tests for the assert_calendar_in_owner_scope write-side guard."""

    @pytest.fixture
    def organization(self):
        """Create a test organization."""
        return baker.make(Organization, name="Test Organization")

    @pytest.fixture
    def user(self, organization):
        """Create a test user."""
        return baker.make(User, email="provider@example.com")

    @pytest.fixture
    def other_user(self, organization):
        """Create another test user (different provider)."""
        return baker.make(User, email="other_provider@example.com")

    @pytest.fixture
    def membership(self, organization, user):
        """Create an active membership for the main user."""
        return baker.make(
            OrganizationMembership, user=user, organization=organization, is_active=True
        )

    @pytest.fixture
    def other_membership(self, organization, other_user):
        """Create an active membership for the other user."""
        return baker.make(
            OrganizationMembership, user=other_user, organization=organization, is_active=True
        )

    @pytest.fixture
    def owned_calendar(self, organization, user):
        """Create a calendar owned by the main user."""
        calendar = baker.make(
            Calendar, organization=organization, name="Owned Calendar", external_id="owned-cal"
        )
        OrganizationMembership.objects.get_or_create(user=user, organization=organization)
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=user.id,
            organization=organization,
        )
        return calendar

    @pytest.fixture
    def other_calendar(self, organization, other_user):
        """Create a calendar owned by the other user (cross-owner target)."""
        calendar = baker.make(
            Calendar,
            organization=organization,
            name="Other Calendar",
            external_id="other-cal",
        )
        OrganizationMembership.objects.get_or_create(user=other_user, organization=organization)
        baker.make(
            CalendarOwnership,
            calendar=calendar,
            membership_user_id=other_user.id,
            organization=organization,
        )
        return calendar

    @pytest.fixture
    def org_wide_system_user(self, organization):
        """Create an org-wide system user (no owner scope)."""
        auth_service = PublicAPIAuthService()
        system_user, _ = auth_service.create_system_user(
            integration_name="org_wide_write_token", organization=organization
        )
        return system_user

    @pytest.fixture
    def scoped_system_user(self, organization, membership):
        """Create a system user scoped to the main user's membership."""
        return baker.make(
            SystemUser,
            organization=organization,
            scoped_to_membership_user_id=membership.user_id,
            integration_name="scoped_write_token",
        )

    def test_no_raise_when_system_user_is_none(self, organization, owned_calendar):
        """Guard is a no-op when system_user is None (no auth context)."""
        # Must not raise regardless of calendar_id
        assert_calendar_in_owner_scope(None, organization, owned_calendar.id)

    def test_no_raise_for_org_wide_token(self, org_wide_system_user, organization, other_calendar):
        """Guard is a no-op for org-wide tokens (scoped_calendar_ids returns None).

        An org-wide token can target any calendar without the guard raising — this is
        the no-regression assertion that ensures org-wide behavior is structurally unchanged.
        """
        assert_calendar_in_owner_scope(org_wide_system_user, organization, other_calendar.id)

    def test_no_raise_for_in_scope_calendar(self, scoped_system_user, organization, owned_calendar):
        """Guard does not raise when the calendar is within the token owner's scope."""
        assert_calendar_in_owner_scope(scoped_system_user, organization, owned_calendar.id)

    def test_raises_does_not_exist_for_out_of_scope_calendar(
        self, scoped_system_user, organization, other_calendar
    ):
        """Guard raises Calendar.DoesNotExist for a cross-owner calendar_id when scoped.

        The raised exception uses the same message as a genuinely missing calendar to
        prevent existence leaks.
        """
        with pytest.raises(
            Calendar.DoesNotExist, match=r"Calendar matching query does not exist\."
        ):
            assert_calendar_in_owner_scope(scoped_system_user, organization, other_calendar.id)

    def test_raises_for_nonexistent_calendar_id_when_scoped(self, scoped_system_user, organization):
        """Guard raises Calendar.DoesNotExist for a nonexistent calendar_id when scoped.

        A nonexistent id is not in the allowed set, so the same exception is raised.
        """
        nonexistent_id = 999999
        with pytest.raises(Calendar.DoesNotExist):
            assert_calendar_in_owner_scope(scoped_system_user, organization, nonexistent_id)
