"""Tests for OrganizationMembership model additions (Phase 1)."""

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from model_bakery import baker
from rest_framework import status
from rest_framework.test import APIClient

from organizations.models import Organization, OrganizationMembership, OrganizationRole


User = get_user_model()


@pytest.mark.django_db
class TestOrganizationMembershipIsActive:
    """Unit tests for the is_active field on OrganizationMembership."""

    def test_is_active_defaults_to_true(self):
        """A freshly created OrganizationMembership is active by default."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = OrganizationMembership.objects.create(user=user, organization=org)
        assert membership.is_active is True

    def test_is_active_can_be_set_false(self):
        """is_active can be set to False to deactivate a membership."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = OrganizationMembership.objects.create(user=user, organization=org)
        membership.is_active = False
        membership.save()

        refreshed = OrganizationMembership.objects.get(pk=membership.pk)
        assert refreshed.is_active is False

    def test_factory_can_produce_inactive_membership(self):
        """baker can create an OrganizationMembership with is_active=False."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            is_active=False,
        )
        assert membership.is_active is False

    def test_factory_produces_active_by_default(self):
        """baker creates an active membership when is_active is not specified."""
        user = baker.make(User)
        org = baker.make(Organization)
        membership = baker.make(OrganizationMembership, user=user, organization=org)
        assert membership.is_active is True


@pytest.mark.django_db
class TestInactiveMembershipGating:
    """Integration tests: inactive membership is treated as gated at tenant endpoints."""

    def _make_inactive_member_client(self):
        """Create a user with an inactive membership, return (user, APIClient)."""
        from users.factories import UserFactory

        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=False,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return user, org, client

    def _make_active_member_client(self):
        """Create a user with an active membership, return (user, org, APIClient)."""
        from users.factories import UserFactory

        user = UserFactory().create_user()
        org = baker.make(Organization)
        baker.make(
            OrganizationMembership,
            user=user,
            organization=org,
            role=OrganizationRole.MEMBER,
            is_active=True,
        )
        client = APIClient()
        client.force_authenticate(user=user)
        return user, org, client

    def test_inactive_membership_gets_empty_list_on_calendar_endpoint(self):
        """An inactive member gets an empty calendar list — not 500 or real data."""
        from calendar_integration.models import Calendar

        _user, org, client = self._make_inactive_member_client()
        baker.make(Calendar, organization=org)

        url = reverse("api:Calendars-list")
        response = client.get(url)

        # Clean response — empty list, not 500
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["results"] == []

    def test_active_membership_sees_calendars(self):
        """An active member can see their organization's calendars."""
        from calendar_integration.models import Calendar

        _user, org, client = self._make_active_member_client()
        baker.make(Calendar, organization=org)

        url = reverse("api:Calendars-list")
        response = client.get(url)

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert len(body["results"]) == 1

    def test_inactive_membership_denied_on_invitations_endpoint(self):
        """An inactive member is denied access to the invitations endpoint."""
        _user, _org, client = self._make_inactive_member_client()

        url = reverse("api:OrganizationInvitations-list")
        response = client.get(url)

        # OrganizationInvitationPermission now gates inactive members
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_reactivation_restores_access(self):
        """Reactivating a membership restores tenant access.

        Note: the client re-authenticates after reactivation so that the request
        user object does not carry a stale cached membership (Django caches the
        reverse OneToOne result on the user instance).
        """
        from calendar_integration.models import Calendar

        user, org, client = self._make_inactive_member_client()
        baker.make(Calendar, organization=org)

        # Verify inactive = empty
        url = reverse("api:Calendars-list")
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"] == []

        # Reactivate in the DB
        OrganizationMembership.objects.filter(user=user).update(is_active=True)

        # Re-authenticate with a fresh user instance so the cached membership is not stale
        user.refresh_from_db()
        client.force_authenticate(user=user)

        # Verify active = data visible
        response = client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.json()["results"]) == 1
