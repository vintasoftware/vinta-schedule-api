from django.db import connection

import pytest

from s3direct_overrides.model_fields import S3DirectFileField, S3DirectImageField
from s3direct_overrides.models import S3DirectModel
from s3direct_overrides.utils import adjust_s3_media_url, generate_unique_id, get_signed_url


@pytest.fixture(scope="function")
def setup_test_model(db):
    class TestS3DirectModel(S3DirectModel):
        file_field = S3DirectFileField(dest="test_files")
        image_field = S3DirectImageField(dest="test_images")

        class Meta:
            app_label = "s3direct_overrides"

    # Create the table for our test model
    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(TestS3DirectModel)

    yield TestS3DirectModel

    # Clean up the table after the test
    with connection.schema_editor() as schema_editor:
        schema_editor.delete_model(TestS3DirectModel)


@pytest.mark.django_db
def test_generate_unique_id():
    unique_id = generate_unique_id()
    assert isinstance(unique_id, str)
    assert len(unique_id) > 0


@pytest.mark.django_db
def test_adjust_s3_media_url_valid(monkeypatch):
    monkeypatch.setattr("django.conf.settings.S3DIRECT_ENDPOINT", "https://s3.example.com")
    monkeypatch.setattr("django.conf.settings.AWS_MEDIA_BUCKET_NAME", "bucket")
    monkeypatch.setattr("django.conf.settings.MEDIA_URL", "/media/")
    url = "https://s3.example.com/bucket/test.jpg"
    result = adjust_s3_media_url(url)
    assert result == "/media/test.jpg"


@pytest.mark.django_db
def test_adjust_s3_media_url_media(monkeypatch):
    monkeypatch.setattr("django.conf.settings.MEDIA_URL", "/media/")
    url = "/media/test.jpg"
    result = adjust_s3_media_url(url)
    # adjust_s3_media_url returns None for a relative url (no scheme)
    assert result is None


@pytest.mark.django_db
def test_adjust_s3_media_url_invalid():
    assert adjust_s3_media_url(None) is None
    assert adjust_s3_media_url("") is None
    assert adjust_s3_media_url("not a url") is None


@pytest.mark.django_db
def test_get_signed_url(monkeypatch):
    class DummyStorage:
        def url(self, name, expire):
            return f"signed:{name}:{expire}"

    monkeypatch.setattr("common.media_storage_backend.MediaStorage", DummyStorage)
    monkeypatch.setattr("django.conf.settings.S3DIRECT_ENDPOINT", "https://s3.example.com")
    monkeypatch.setattr("django.conf.settings.AWS_MEDIA_BUCKET_NAME", "bucket")
    monkeypatch.setattr("django.conf.settings.MEDIA_URL", "/media/")
    # S3 endpoint
    url = "https://s3.example.com/bucket/test.jpg"
    assert get_signed_url(url) == "signed:test.jpg:7200"
    # MEDIA_URL
    url = "/media/test.jpg"
    assert get_signed_url(url) == "signed:test.jpg:7200"
    # Other (should include bucket in key)
    url = "https://other.com/bucket/test.jpg"
    assert get_signed_url(url) == "signed:/bucket/test.jpg:7200"


@pytest.mark.django_db
def test_s3direct_model_save_and_signed_properties(monkeypatch, setup_test_model):
    monkeypatch.setattr("django.conf.settings.S3DIRECT_ENDPOINT", "https://s3.example.com")
    monkeypatch.setattr("django.conf.settings.AWS_MEDIA_BUCKET_NAME", "bucket")
    monkeypatch.setattr("django.conf.settings.MEDIA_URL", "/media/")
    monkeypatch.setattr(
        "common.media_storage_backend.MediaStorage",
        type("DummyStorage", (), {"url": lambda self, name, expire: f"signed:{name}:{expire}"}),
    )

    # Use the test model from the fixture
    model = setup_test_model
    obj = model()
    obj.file_field = "/media/testfile.txt"
    obj.image_field = "/media/testimage.jpg"
    obj.save()

    # After save, fields should be adjusted
    assert obj.file_field == "/media/testfile.txt"
    assert obj.image_field == "/media/testimage.jpg"

    # Signed properties should work
    assert obj.signed_file_field == "signed:testfile.txt:7200"
    assert obj.signed_image_field == "signed:testimage.jpg:7200"
