from urllib.parse import unquote

from django import forms
from django.conf import settings

from s3direct_overrides.form_widgets import S3DirectWidget


class S3DirectField(forms.CharField):
    def __init__(self, *args, **kwargs):
        dest = kwargs.pop("dest", None)
        super().__init__(*args, **kwargs)
        self.widget = S3DirectWidget(dest=dest)

    def clean(self, value, *args, **kwargs):
        # Remove query strings to prevent adding signature and expiration twice
        if value and isinstance(value, str) and "?" in unquote(value):
            value = unquote(value).split("?")[0]

        # remove the protocol part if it exists
        if value and isinstance(value, str) and "://" in value:
            value = value.split("://", 1)[-1]

            # remove the host part if it exists
            if not value.startswith("/"):
                value = "/" + value.split("/", 1)[-1]

            # remove the bucket name if it exists
            if value.startswith("/" + settings.S3_BUCKET_NAME):
                value = "/" + value.lstrip("/" + settings.S3_BUCKET_NAME)

        return super().clean(value, *args, **kwargs)
