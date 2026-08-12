"""Shared ``django-virtual-models`` plumbing.

See ``AGENTS.md`` → Architecture → Django Virtual Models for what virtual models
are for; this module only holds the base class every organization-scoped one
should use.
"""

from typing import Any

from django.db.models import Model, QuerySet

import django_virtual_models as v
from vinta_orgs.managers import OrganizationScopedManagerMixin


class OrganizationScopedVirtualModel(v.VirtualModel):
    """Virtual model whose prefetch queryset is not organization-scoped.

    ``VirtualModel.get_prefetch_queryset`` returns ``self.manager.all()``, and
    ``self.manager`` defaults to ``Meta.model._default_manager`` -- which, on a
    model using ``SingleOrganizationModelMixin``, scopes to the organization
    bound to the current context and, under ``STRICT_ORGANIZATION_FILTER``,
    refuses when none is.

    A prefetch queryset is the wrong place to ask for that binding. It is built
    while a serializer is being optimized -- before any of the rows it will fetch
    are known -- and it only ever runs restricted to the parent rows the outer
    queryset returned, which the caller already scoped. Where the relation is
    declared with ``OrganizationSafeForeignKey``, the prefetch's own ``WHERE``
    clause carries ``organization`` as well, taken from the parent row. So the
    ambient organization adds no restriction the prefetch does not already have,
    while demanding one turns "optimize this serializer" into something that only
    works inside a bound context.

    Only the *organization* scoping is dropped. A manager that narrows its
    default view for some other reason -- ``BlockedTimeManager`` and
    ``AvailableTimeManager`` hide group-slot-scoped rows -- keeps doing so
    through :meth:`OrganizationScopedManager.unscoped_default_queryset`, so this
    changes which organizations a prefetch may reach and nothing else.

    A manager that does not scope implicitly (an explicitly passed ``manager=``
    that is a plain ``Manager``, for instance) is left alone.
    """

    def get_prefetch_queryset(self, user: Model | None = None, **kwargs: Any) -> QuerySet:
        # Tested by behaviour rather than by identity against
        # ``model_cls._default_manager``: ``VirtualModel.get_fields`` deep-copies
        # every declared field, and with it the manager instance, so an identity
        # check silently stops matching for exactly the nested prefetches this
        # exists to serve.
        if isinstance(self.manager, OrganizationScopedManagerMixin):
            unscoped = getattr(self.manager, "unscoped_default_queryset", None)
            if unscoped is not None:
                return unscoped()
            return self.manager.unscoped()

        return super().get_prefetch_queryset(user=user, **kwargs)
