from typing import Any

from django.db import models
from django.utils.translation import gettext_lazy as _

from model_utils.fields import AutoCreatedField, AutoLastModifiedField
from organizations.fields import get_organization_safe_relations


class SafeRelationNullInitMixin(models.Model):
    """Stop ``Model(relation=None)`` from clearing an explicit ``organization``.

    An organization-safe relation is two fields: the concrete ``<name>_fk`` and
    a non-concrete ``<name>`` joining ``(<name>_fk, organization)``. Django's
    forward descriptor writes *both* of a relation's local columns when it is
    assigned, which is deliberate on the package's part -- assigning
    ``comment.article = article`` copies the article's organization onto the
    comment, so the row cannot be built pointing across tenants. That is why
    ``SingleOrganizationModelMixin.__init__`` leaves an instance-valued kwarg
    for Django's descriptor rather than rewriting it onto ``<name>_fk``.

    Assigning ``None`` goes down the same path and writes ``None`` to both
    columns -- so ``BookingPolicy(organization=org, calendar=None)``, which is
    what a constructor with optional targets looks like, silently discards
    ``org``. The row then fails ``organization_id NOT NULL``, or (before this
    migration turned every scoped model's ``save()`` into an explicit check)
    landed as a plain ``IntegrityError`` from Postgres.

    ``None`` carries no organization to copy, so rewriting just that case onto
    the concrete field loses nothing and keeps the rest of the package's
    behavior intact. This restores exactly what the retired ``OrganizationModel``
    did (it rewrote *every* value onto ``<name>_fk``), for the one value where
    the package's choice is destructive rather than protective.

    Ordered **after** ``SingleOrganizationModelMixin`` in the bases of every
    scoped model: that mixin's ``__init__`` runs first, rewrites ``<name>_id=``
    onto ``<name>_fk_id=``, and passes the rest down to this one.
    """

    class Meta:
        abstract = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if kwargs:
            for name in get_organization_safe_relations(self.__class__):
                if name in kwargs and kwargs[name] is None:
                    kwargs.pop(name)
                    kwargs.setdefault(f"{name}_fk", None)
        super().__init__(*args, **kwargs)


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
