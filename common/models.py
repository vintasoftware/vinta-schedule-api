from typing import Any

from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models.fields.related import ForeignObject
from django.utils.translation import gettext_lazy as _

from model_utils.fields import AutoCreatedField, AutoLastModifiedField


#: The field every organization-scoped model in this project relates to
#: ``Organization`` through -- ``SingleOrganizationModelMixin.organization``.
_ORGANIZATION_FIELD_NAME = "organization"

#: model -> {relation name: the concrete fields to clear when it is set to ``None``}.
#: Read on every ``None`` assignment to any attribute of a scoped instance.
_organization_bearing_relations_cache: dict[type[models.Model], dict[str, tuple[str, ...]]] = {}


def get_organization_bearing_relations(
    model: type[models.Model],
) -> dict[str, tuple[str, ...]]:
    """Relations of ``model`` that write ``organization`` when they are assigned.

    Recognized by shape rather than by field class: any non-concrete
    ``ForeignObject`` whose ``local_related_fields`` include the model's
    ``organization`` field. Django's forward descriptor writes *every* local
    related field on assignment, so those -- and only those -- can clear
    ``organization`` when assigned ``None``.

    Deliberately broader than ``vinta_orgs.fields.get_organization_safe_relations``,
    which recognizes a relation only by a concrete ``<name>_fk`` sibling. That
    misses :class:`common.fields.OrganizationMembershipForeignKey`, whose concrete
    column is ``<name>_user_id`` -- so ``policy.membership = None`` was still
    nulling ``organization_id`` on ``BookingPolicy`` / ``EventAttendance`` /
    ``ExternalEventChangeRequest`` / ``CalendarOwnership`` / ``CalendarManagementToken``.

    The value is the *other* local related fields, by field name: assigning one of
    those names is what clears the key while leaving ``organization`` alone. A
    field name is used rather than an attname so a concrete ``ForeignKey``
    (``<name>_fk``) goes through its own descriptor and drops its cached instance
    too; for a plain column (``<name>_user_id``) the two are the same string.
    """
    try:
        return _organization_bearing_relations_cache[model]
    except KeyError:
        pass

    relations: dict[str, tuple[str, ...]] = {}

    try:
        model._meta.get_field(_ORGANIZATION_FIELD_NAME)
    except FieldDoesNotExist:
        # Not an organization-scoped model; nothing here can clear an
        # organization it does not have.
        _organization_bearing_relations_cache[model] = relations
        return relations

    for field in model._meta.get_fields():
        # ``ForeignKey`` is a ``ForeignObject`` subclass, but it owns a single
        # column and cannot touch ``organization``.
        if not isinstance(field, ForeignObject) or isinstance(field, models.ForeignKey):
            continue

        local_names = [local.name for local in field.local_related_fields]

        if _ORGANIZATION_FIELD_NAME not in local_names:
            continue

        relations[field.name] = tuple(
            name for name in local_names if name != _ORGANIZATION_FIELD_NAME
        )

    _organization_bearing_relations_cache[model] = relations
    return relations


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

    Which relations qualify is decided by
    :func:`get_organization_bearing_relations`, not by the package's
    ``get_organization_safe_relations``: the latter keys on a concrete
    ``<name>_fk`` sibling and so does not see
    :class:`common.fields.OrganizationMembershipForeignKey` (``membership``,
    ``resolved_by``), whose concrete column is ``<name>_user_id``. Those relations
    join on ``organization`` exactly like a safe relation does and are equally
    destructive to assign ``None`` to -- more so since the flip to
    ``SingleOrganizationModelMixin``, whose ``save()`` re-stamps the row from the
    bound context rather than letting the ``NOT NULL`` fail.

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
        if value is None:
            concrete_names = get_organization_bearing_relations(self.__class__).get(name)
            if concrete_names is not None:
                # The concrete fields own the key columns, so writing them clears
                # the key and leaves ``organization`` alone.
                for concrete_name in concrete_names:
                    super().__setattr__(concrete_name, None)
                # ...and drop any previously loaded object for the relation, so a
                # later read does not hand back the target that was just cleared.
                state = getattr(self, "_state", None)
                if state is not None:
                    state.fields_cache.pop(name, None)
                return
        super().__setattr__(name, value)


class UnscopedUniqueChecksMixin(models.Model):
    """Run Django's uniqueness pre-check across every organization.

    Mix into an organization-scoped model that is edited through a ``ModelForm``
    (today: the ``SystemUser`` admin). ``ModelForm._post_clean`` calls
    ``full_clean()`` / ``validate_unique()``, which probe through
    ``_default_manager`` -- the scoped manager -- and so raise
    ``OrganizationNotFoundError`` wherever no organization is bound, and would
    under-report a clash if one were. See
    :func:`common.managers.unscoped_default_manager` for the full argument.

    Not folded into every scoped model: nothing else in this project runs
    ``full_clean()`` on one (DRF serializers do their own validation and never
    call it), and a mixin on 34 models that changes behaviour for one is harder
    to reason about than a mixin on the one that needs it.
    """

    class Meta:
        abstract = True

    def validate_unique(self, exclude: Any = None) -> None:
        from common.managers import unscoped_default_manager

        with unscoped_default_manager():
            super().validate_unique(exclude=exclude)


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
