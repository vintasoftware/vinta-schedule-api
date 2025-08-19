from django.conf import settings
from django.db import models

from s3direct_overrides.form_fields import S3DirectField
from s3direct_overrides.form_widgets import S3DirectWidget


class S3DirectImageField(models.ImageField):
    def __init__(self, *args, **kwargs):
        self.dest = kwargs.pop("dest", None)
        self.widget = S3DirectWidget(dest=self.dest)
        if self.dest:
            dest_options = settings.S3DIRECT_DESTINATIONS.get(self.dest, {})
            kwargs["upload_to"] = dest_options.get("key_args", "uploads")
        if "max_length" not in kwargs:
            kwargs["max_length"] = 255
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Add back the `dest` argument so Django can recreate the field
        if self.dest:
            kwargs["dest"] = self.dest
        return name, path, args, kwargs

    def get_internal_type(self):
        return models.ImageField().get_internal_type()

    def formfield(self, *args, **kwargs):
        kwargs["widget"] = self.widget
        if "dest" not in kwargs:
            kwargs["dest"] = self.dest
        return S3DirectField(*args, **kwargs)


class S3DirectFileField(models.FileField):
    def __init__(self, *args, **kwargs):
        self.dest = kwargs.pop("dest", None)
        self.widget = S3DirectWidget(dest=self.dest)
        if self.dest:
            dest_options = settings.S3DIRECT_DESTINATIONS.get(self.dest, {})
            kwargs["upload_to"] = dest_options.get("key_args", "uploads")
        if "max_length" not in kwargs:
            kwargs["max_length"] = 255
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        # Add back the `dest` argument so Django can recreate the field
        if "dest" in kwargs:
            dest = kwargs.pop("dest")
            kwargs["dest"] = dest
        return name, path, args, kwargs

    def get_internal_type(self):
        return models.FileField().get_internal_type()

    def formfield(self, *args, **kwargs):
        kwargs["widget"] = self.widget
        if "dest" not in kwargs:
            kwargs["dest"] = self.dest
        return S3DirectField(*args, **kwargs)
