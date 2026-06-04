from unittest.mock import MagicMock, patch

import pytest

from users.factories import ProfileFactory, UserFactory
from users.models import Profile
from users.tasks import download_social_profile_picture


@pytest.mark.django_db
class TestDownloadSocialProfilePicture:
    def _profile_without_picture(self) -> Profile:
        user = UserFactory().create_user(email="pic@example.com")
        return ProfileFactory().create_profile(user=user, profile_picture=None)

    def test_stores_downloaded_image(self):
        profile = self._profile_without_picture()
        response = MagicMock()
        response.headers = {"Content-Type": "image/png"}
        response.content = b"fake-png-bytes"

        with patch("users.tasks.requests.get", return_value=response) as mock_get:
            download_social_profile_picture(profile.pk, "https://example.com/a.png")

        mock_get.assert_called_once()
        profile.refresh_from_db()
        assert profile.profile_picture
        assert profile.profile_picture.name.endswith(".png")

    def test_no_op_when_url_missing(self):
        profile = self._profile_without_picture()
        with patch("users.tasks.requests.get") as mock_get:
            download_social_profile_picture(profile.pk, "")
        mock_get.assert_not_called()

    def test_no_op_when_profile_already_has_picture(self):
        user = UserFactory().create_user(email="has@example.com")
        profile = ProfileFactory().create_profile(user=user)
        profile.profile_picture = "profile_pictures/existing.png"
        profile.save()
        with patch("users.tasks.requests.get") as mock_get:
            download_social_profile_picture(profile.pk, "https://example.com/a.png")
        mock_get.assert_not_called()

    def test_skips_non_image_content_type(self):
        profile = self._profile_without_picture()
        response = MagicMock()
        response.headers = {"Content-Type": "text/html"}
        response.content = b"<html></html>"

        with patch("users.tasks.requests.get", return_value=response):
            download_social_profile_picture(profile.pk, "https://example.com/a.png")

        profile.refresh_from_db()
        assert not profile.profile_picture

    def test_storage_failure_does_not_propagate(self):
        # Avatar storage is best-effort; a storage error must not bubble up and
        # break the (possibly inline / eager) signup that enqueued the task.
        profile = self._profile_without_picture()
        response = MagicMock()
        response.headers = {"Content-Type": "image/png"}
        response.content = b"fake-png-bytes"

        with (
            patch("users.tasks.requests.get", return_value=response),
            patch.object(
                Profile.profile_picture.field.storage,
                "save",
                side_effect=Exception("S3 down"),
            ),
        ):
            # Must not raise.
            download_social_profile_picture(profile.pk, "https://example.com/a.png")

        profile.refresh_from_db()
        assert not profile.profile_picture

    def test_skips_oversized_image(self):
        profile = self._profile_without_picture()
        response = MagicMock()
        response.headers = {"Content-Type": "image/png"}
        response.content = b"x" * (5 * 1024 * 1024 + 1)

        with patch("users.tasks.requests.get", return_value=response):
            download_social_profile_picture(profile.pk, "https://example.com/a.png")

        profile.refresh_from_db()
        assert not profile.profile_picture
