from django.db import models
from django.utils.translation import gettext_lazy as _

from model_utils.fields import AutoCreatedField, AutoLastModifiedField


class IndexedTimeStampedModel(models.Model):
    created = AutoCreatedField(_("created"), db_index=True)
    modified = AutoLastModifiedField(_("modified"), db_index=True)

    class Meta:
        abstract = True


class MetaJsonFieldModel(models.Model):
    meta = models.JSONField(_("meta"), default=dict, blank=True)

    class Meta:
        abstract = True


class BaseModel(IndexedTimeStampedModel, MetaJsonFieldModel):
    class Meta(IndexedTimeStampedModel.Meta, MetaJsonFieldModel.Meta):
        abstract = True
