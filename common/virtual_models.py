"""Shared `django-virtual-models` plumbing.

See `AGENTS.md` → Architecture → Django Virtual Models for what virtual models
are for; this module only holds the base class every organization-scoped one
should use.
"""

from typing import Any

from django.db.models import Model, QuerySet

import django_virtual_models as v


class OrganizationScopedVirtualModel(v.VirtualModel):
    """Virtual model whose prefetch queryset is not organization-scoped.

    ``VirtualModel.get_prefetch_queryset`` returns ``self.manager.all()``, and
    ``self.manager`` defaults to ``Meta.model._default_manager`` -- which, on a
    model using ``SingleOrganizationModelMixin``, scopes to the organization
    bound to the current context and (under ``STRICT_ORGANIZATION_FILTER``)
    refuses when none is.

    A prefetch queryset is the wrong place to ask for that. It is built while a
    serializer is being optimized -- before any of the rows it will fetch are
    known -- and it only ever runs restricted to the parent rows the outer
    queryset returned, which the caller already scoped. Where the relation is
    declared with ``OrganizationSafeForeignKey``, the prefetch's own ``WHERE``
    clause carries ``organization`` too, taken from the parent row. So the
    ambient organization adds no restriction the prefetch does not already
    have, and demanding one turns "optimize this serializer" into a call that
    only works inside a request.

    Reaches for ``original_manager`` only when the virtual model is using the
    model's default manager; an explicitly-passed ``manager=`` is what the
    caller asked for and is left alone.
    """

    def get_prefetch_queryset(self, user: Model | None = None, **kwargs: Any) -> QuerySet:
        if self.manager is self.model_cls._default_manager:
            unscoped_manager = getattr(self.model_cls, "original_manager", None)
            if unscoped_manager is not None:
                return unscoped_manager.all()

        return super().get_prefetch_queryset(user=user, **kwargs)
