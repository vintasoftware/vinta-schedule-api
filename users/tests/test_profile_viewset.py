from django.urls import reverse

import pytest
from rest_framework import status

from users.factories import UserFactory
from users.fixtures import *  # noqa: F401


@pytest.mark.django_db
class TestProfileViewSet:
    """Test suite for the ProfileViewSet."""

    def test_success_get_profile_authenticated_me(self, auth_client, user):
        """Test retrieving authenticated user's profile."""
        url = reverse("api:Profile-detail", kwargs={"pk": "me"})
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == user.profile.pk
        assert response.data["first_name"] == user.profile.first_name
        assert response.data["last_name"] == user.profile.last_name

    def test_failure_get_profile_unauthenticated_me(self, anonymous_client):
        """Test retrieving profile without authentication should fail."""
        url = reverse("api:Profile-detail", kwargs={"pk": "me"})
        response = anonymous_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_success_get_profile_authenticated_other_user(self, auth_client, user):
        """Test retrieving authenticated user's profile."""
        other_user = UserFactory().create_user(email="otheruser@example.com")

        url = reverse("api:Profile-detail", kwargs={"pk": other_user.profile.pk})
        response = auth_client.get(url)

        assert response.status_code == status.HTTP_200_OK
        assert response.data["id"] == other_user.profile.pk
        assert response.data["first_name"] == other_user.profile.first_name
        assert response.data["last_name"] == other_user.profile.last_name

    def test_failure_get_profile_unauthenticated_other_user(self, anonymous_client):
        """Test retrieving profile without authentication should fail."""
        other_user = UserFactory().create_user(email="otheruser@example.com")

        url = reverse("api:Profile-detail", kwargs={"pk": other_user.profile.pk})
        response = anonymous_client.get(url)

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_success_update_profile_me(self, auth_client, user, profile_data):
        """Test updating the user's profile."""
        url = reverse("api:Profile-detail", kwargs={"pk": "me"})
        response = auth_client.patch(url, profile_data, format="json")

        assert response.status_code == status.HTTP_200_OK

        # Refresh the profile from the database
        user.profile.refresh_from_db()

        assert user.profile.first_name == profile_data["first_name"]
        assert user.profile.last_name == profile_data["last_name"]

        # Check that the response contains the updated data
        assert response.data["first_name"] == profile_data["first_name"]
        assert response.data["last_name"] == profile_data["last_name"]

    def test_success_partial_update_profile_me(self, auth_client, user):
        """Test partially updating the profile with just the first name."""
        url = reverse("api:Profile-detail", kwargs={"pk": "me"})
        data = {"first_name": "NewFirstName"}

        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_200_OK

        # Refresh the profile from the database
        user.profile.refresh_from_db()

        assert user.profile.first_name == "NewFirstName"
        assert response.data["first_name"] == "NewFirstName"

        # Last name should remain unchanged
        original_last_name = user.profile.last_name
        assert response.data["last_name"] == original_last_name

    def test_success_put_profile_me(self, auth_client, user, profile_data):
        """Test completely replacing the profile with PUT."""
        url = reverse("api:Profile-detail", kwargs={"pk": "me"})
        response = auth_client.put(url, profile_data, format="json")

        assert response.status_code == status.HTTP_200_OK

        # Refresh the profile from the database
        user.profile.refresh_from_db()

        assert user.profile.first_name == profile_data["first_name"]
        assert user.profile.last_name == profile_data["last_name"]

    def test_failure_profile_update_other_user(self, auth_client):
        """Test other user trying to update profile should fail."""
        other_user = UserFactory().create_user(email="otheruser@example.com")

        url = reverse("api:Profile-detail", kwargs={"pk": other_user.profile.pk})

        data = {"first_name": "Test Update Other User"}
        response = auth_client.patch(url, data, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
