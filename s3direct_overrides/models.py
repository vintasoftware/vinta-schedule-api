from collections.abc import Callable

from django.db import models
from django.db.models.base import ModelBase

from s3direct.fields import S3DirectField as S3DirectModelField

from s3direct_overrides.utils import adjust_s3_media_url, get_signed_url


def _get_signed_url_from_field(self, field_name):
    value = getattr(self, field_name, None)
    # Convert FieldFile to string if needed
    if hasattr(value, "name"):
        value = value.name
    if not value:
        return None
    return get_signed_url(str(value))


def _get_signed_url_factory(field_name):
    def _get_signed_url_property(self):
        return _get_signed_url_from_field(self, field_name)

    return _get_signed_url_property


class S3DirectModelMetaClass(ModelBase):
    get_signed_file: Callable[["S3DirectModel", str], str]

    def __new__(cls, *args, **kwargs):  # pylint: disable=bad-mcs-classmethod-argument
        klas = super().__new__(cls, *args, **kwargs)
        for field in klas._meta.fields:
            # Check for both S3DirectModelField and our custom fields
            if isinstance(field, S3DirectModelField) or hasattr(field, "dest"):
                setattr(
                    klas,
                    f"signed_{field.name}",
                    property(_get_signed_url_factory(field.name)),
                )
        klas.get_signed_file = _get_signed_url_from_field
        return klas


class S3DirectModel(models.Model, metaclass=S3DirectModelMetaClass):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs) -> None:
        for field in self._meta.fields:
            if isinstance(field, S3DirectModelField) or hasattr(field, "dest"):
                current_value = getattr(self, field.name, None)
                # Convert FieldFile to string if needed
                if current_value is not None and hasattr(current_value, "name"):
                    current_value = current_value.name
                if current_value:  # Only adjust if there's a value
                    adjusted_value = adjust_s3_media_url(str(current_value))
                    if adjusted_value is not None:  # Only set if adjustment returned a value
                        setattr(self, field.name, adjusted_value)
        return super().save(*args, **kwargs)
