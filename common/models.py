from typing import Any

from django.db import models
from django.utils.translation import gettext_lazy as _

from model_utils.fields import AutoCreatedField, AutoLastModifiedField
from vinta_orgs.fields import get_organization_safe_relations


class SafeRelationNullInitMixin(models.Model):
    """Stop ``instance.relation = None`` from clearing the row's ``organization``.

    An organization-safe relation is two fields: the concrete ``<name>_fk`` and a
    non-concrete ``<name>`` joining ``(<name>_fk, organization)``. Django's
    forward descriptor writes *both* of a relation's local columns when it is
    assigned, which is deliberate on the package's part -- ``comment.article =
    article`` copies the article's organization onto the comment, so the row
    cannot be built pointing across tenants. That is exactly why
    ``SingleOrganizationModelMixin.__init__`` leaves an instance-valued keyword
    argument for Django's descriptor instead of rewriting it onto ``<name>_fk``.

    Assigning ``None`` takes the same path and writes ``None`` to *both* columns,
    which is destructive rather than protective:

    * ``BookingPolicy(organization=org, calendar=None)`` -- the ordinary shape of
      a constructor with optional targets, and one this codebase uses -- silently
      discards ``org``.
    * ``window.recurrence_rule = None`` (how a recurring window is made
      non-recurring) discards the organization of a row that is *already
      persisted*, and the following ``save()`` either stamps it with whatever
      organization happens to be bound or raises ``OrganizationNotFoundError``.

    ``None`` carries no organization to copy, so rewriting that one case onto the
    concrete field loses nothing and leaves the rest of the package's behaviour
    intact. This restores, for the one value where the package's choice is
    destructive, what the retired ``OrganizationModel`` did for every value.

    Implemented in ``__setattr__`` rather than in ``__init__`` because both
    entry points go through it: ``Model.__init__`` assigns a relation-valued
    keyword argument with ``setattr(self, field.name, value)``. The ``value is
    None`` test is first so a non-null assignment -- every other attribute set on
    every instance -- pays one identity comparison and nothing else.

    Ordered **after** ``SingleOrganizationModelMixin`` in the bases of every
    scoped model, so that mixin's ``__init__`` runs first (rewriting ``<name>_id=``
    onto ``<name>_fk_id=``) and passes the rest down here.
    """

    class Meta:
        abstract = True

    def __setattr__(self, name: str, value: Any) -> None:
        if value is None and name in get_organization_safe_relations(self.__class__):
            # The concrete field owns the column, so writing it clears the key and
            # leaves ``organization`` alone.
            super().__setattr__(f"{name}_fk", None)
            # ...and drop any previously loaded object for the safe relation, so a
            # later read does not hand back the target that was just cleared.
            state = getattr(self, "_state", None)
            if state is not None:
                state.fields_cache.pop(name, None)
            return
        super().__setattr__(name, value)


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
