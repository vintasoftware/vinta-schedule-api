from typing import Any, ClassVar
from urllib.parse import urlparse

from django.conf import settings
from django.core.validators import URLValidator
from django.utils.translation import gettext_lazy as _

from rest_framework import serializers
from rest_framework.fields import empty

from s3direct_overrides.utils import get_signed_url


class S3DirectField(serializers.CharField):
    default_error_messages: ClassVar[dict[str, Any]] = {"invalid": _("Enter a valid URL.")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._url_validator = URLValidator(message=self.error_messages["invalid"])

    def run_validation(self, data=empty):
        (is_empty_value, data) = self.validate_empty_values(data)
        if is_empty_value:
            return data
        # Validate URL format on raw input before normalizing to a key
        if data and "://" in str(data):
            self._url_validator(data)
        return self.to_internal_value(data)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not value or "://" not in value:
            return value
        # Strip scheme + host + bucket prefix; store only the S3 key
        parsed = urlparse(value)
        path = parsed.path.lstrip("/")
        bucket = getattr(settings, "AWS_MEDIA_BUCKET_NAME", "")
        if bucket and path.startswith(f"{bucket}/"):
            path = path[len(bucket) + 1 :]
        return path

    def to_representation(self, value):
        if not value:
            return None
        return get_signed_url(value)
