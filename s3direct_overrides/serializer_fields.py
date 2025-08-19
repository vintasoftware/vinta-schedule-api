from typing import Any, ClassVar

from django.conf import settings
from django.core.validators import URLValidator as OriginalURLValidator
from django.utils.translation import gettext_lazy as _

from rest_framework import serializers

from s3direct_overrides.utils import get_signed_url


class URLValidator(OriginalURLValidator):
    def __call__(self, value):
        if not settings.USE_MINIO or not value.startswith(settings.MINIO_ENDPOINT):
            super().__call__(value)


class S3DirectField(serializers.CharField):
    default_error_messages: ClassVar[dict[str, Any]] = {"invalid": _("Enter a valid URL.")}

    def __init__(
        self,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        validator = URLValidator(message=self.error_messages["invalid"])
        self.validators.append(validator)

    def to_representation(self, value):
        return get_signed_url(value)
