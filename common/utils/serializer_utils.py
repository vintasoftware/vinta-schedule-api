from django.db.models import Model

import django_virtual_models as v
from rest_framework import serializers
from s3direct.fields import S3DirectField as S3DirectModelField

from s3direct_overrides.form_fields import S3DirectField


def update_model_instance_from_dict(instance: Model, data: dict) -> Model:
    for key, value in data.items():
        setattr(instance, key, value)
    return instance


class ModelSerializer(serializers.ModelSerializer):
    serializer_field_mapping = {  # noqa: RUF012
        **serializers.ModelSerializer.serializer_field_mapping,
        **{S3DirectModelField: S3DirectField},
    }


class VirtualModelSerializer(v.VirtualModelSerializerMixin, ModelSerializer):
    pass
