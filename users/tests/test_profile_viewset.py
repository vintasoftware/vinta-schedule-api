from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.urls import reverse

import pytest
from rest_framework import status
from s3direct.utils import AWSCredentials

from users.factories import UserFactory
from users.fixtures import *  # noqa: F401


S3_TEST_SETTINGS = {
    "AWS_STORAGE_BUCKET_NAME": "test-bucket",
    "AWS_S3_REGION_NAME": "us-east-1",
    "AWS_S3_ENDPOINT_URL": "https://s3.us-east-1.amazonaws.com",
    "AWS_ACCESS_KEY_ID": "test-access-key",
    "AWS_SECRET_ACCESS_KEY": "test-secret-key",
}


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


@pytest.mark.django_db
class TestProfilePictureUploadParams:
    """Test suite for the profile_picture_upload_params action."""

    def _url(self, pk):
        return reverse("api:Profile-profile-picture-upload-params", kwargs={"pk": pk})

    def _valid_payload(self):
        return {"file_name": "avatar.jpg", "file_type": "image/jpeg", "file_size": 1024}

    @patch("users.views.get_aws_credentials")
    def test_success_me(self, mock_creds, auth_client, settings):
        mock_creds.return_value = AWSCredentials(
            token=None, secret_key="secret", access_key="AKIATEST"
        )
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)

        response = auth_client.post(self._url("me"), self._valid_payload(), format="json")

        assert response.status_code == status.HTTP_200_OK
        data = response.data
        assert "object_key" in data
        assert data["object_key"].startswith("uploads/profile_pictures/")
        assert data["upload_url"].startswith("https://")
        assert "test-bucket" in data["upload_url"]
        assert data["expires_in"] == 900

    @patch("users.views.get_aws_credentials")
    def test_returns_a_presigned_url_the_browser_can_put_to_unaided(
        self, mock_creds, auth_client, settings
    ):
        """The response must carry a complete SigV4 presigned URL.

        The SPA authenticates with JWT and cannot reach s3direct's session+CSRF signing
        view, so it has no way to sign a request itself. Everything S3 needs has to be
        in this URL's query string or the upload PUT goes out unsigned and S3 answers 403.
        """
        mock_creds.return_value = AWSCredentials(
            token=None, secret_key="secret", access_key="AKIATEST"
        )
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)

        response = auth_client.post(self._url("me"), self._valid_payload(), format="json")

        assert response.status_code == status.HTTP_200_OK
        upload_url = response.data["upload_url"]

        parsed = urlparse(upload_url)
        query = parse_qs(parsed.query)

        assert parsed.path.endswith(response.data["object_key"])
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-Signature"][0]
        assert query["X-Amz-Credential"][0].startswith("AKIATEST/")
        assert "us-east-1/s3/aws4_request" in query["X-Amz-Credential"][0]
        assert query["X-Amz-Date"][0]
        assert int(query["X-Amz-Expires"][0]) == response.data["expires_in"]

    @patch("users.views.get_aws_credentials")
    def test_presigned_url_signs_content_type_but_never_an_acl(
        self, mock_creds, auth_client, settings
    ):
        """No `x-amz-acl` anywhere: the media bucket has ACLs disabled (BucketOwnerEnforced).

        Content-Type is signed so the type is fixed when the URL is issued and cannot be
        swapped on the PUT.
        """
        mock_creds.return_value = AWSCredentials(
            token=None, secret_key="secret", access_key="AKIATEST"
        )
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)

        response = auth_client.post(self._url("me"), self._valid_payload(), format="json")

        upload_url = response.data["upload_url"]
        signed_headers = parse_qs(urlparse(upload_url).query)["X-Amz-SignedHeaders"][0]

        assert "x-amz-acl" not in signed_headers
        assert "acl" not in upload_url.lower()
        assert "content-type" in signed_headers

    @patch("users.views.get_aws_credentials")
    def test_response_no_longer_leaks_aws_credentials_to_the_browser(
        self, mock_creds, auth_client, settings
    ):
        """The presigned URL replaces the raw key id the old handshake had to expose."""
        mock_creds.return_value = AWSCredentials(
            token=None, secret_key="secret", access_key="AKIATEST"
        )
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)

        response = auth_client.post(self._url("me"), self._valid_payload(), format="json")

        assert "access_key_id" not in response.data
        assert "session_token" not in response.data

    @patch("users.views.get_aws_credentials")
    def test_success_by_pk(self, mock_creds, auth_client, user, settings):
        mock_creds.return_value = AWSCredentials(
            token=None, secret_key="secret", access_key="AKIATEST"
        )
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)

        url = self._url(user.profile.pk)
        response = auth_client.post(url, self._valid_payload(), format="json")

        assert response.status_code == status.HTTP_200_OK

    def test_failure_unauthenticated(self, anonymous_client, settings):
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)

        response = anonymous_client.post(self._url("me"), self._valid_payload(), format="json")

        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @patch("users.views.get_aws_credentials")
    def test_failure_other_user_profile(self, mock_creds, auth_client, settings):
        mock_creds.return_value = AWSCredentials(
            token=None, secret_key="secret", access_key="AKIATEST"
        )
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)
        other_user = UserFactory().create_user(email="otheruser2@example.com")

        response = auth_client.post(
            self._url(other_user.profile.pk), self._valid_payload(), format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    @patch("users.views.get_aws_credentials")
    def test_failure_missing_file_name(self, mock_creds, auth_client, settings):
        mock_creds.return_value = AWSCredentials(
            token=None, secret_key="secret", access_key="AKIATEST"
        )
        for k, v in S3_TEST_SETTINGS.items():
            setattr(settings, k, v)

        payload = {"file_type": "image/jpeg", "file_size": 1024}
        response = auth_client.post(self._url("me"), payload, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
