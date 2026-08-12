from collections.abc import Iterable
from typing import Any

from django.db.models import QuerySet

from vinta_orgs.managers import SingleOrganizationModelManager


class OrganizationScopedManager(SingleOrganizationModelManager):
    """Default manager for an organization-scoped model in this project.

    Reads are the package's: ``objects`` scopes to the organization bound to the
    current context and, with ``STRICT_ORGANIZATION_FILTER = True``, an unbound
    read raises ``OrganizationNotFoundError`` instead of quietly returning
    nothing. Two things the package leaves to the project are decided here.

    **Related managers are not scoped.** Django builds the reverse accessor for
    a relation (``event.attendances``, ``calendar.syncs``, ``group.slots``) by
    subclassing the target model's ``_default_manager`` *class*, so inheriting
    the scoped ``get_queryset`` unchanged would make every reverse traversal
    demand an ambient organization on top of the parent row it is already
    restricted to. There is nothing for the ambient organization to add there:
    the queryset is filtered to one parent instance, and where the relation was
    declared with ``OrganizationSafeForeignKey`` that filter is on
    ``(<name>_fk, organization)`` -- the organization is already in the ``WHERE``
    clause, taken from the parent row rather than from ambient state. A second,
    ambient condition can only be redundant (same organization) or empty the
    result (different one), and the second case means the caller already holds an
    object from an organization it is not scoped to: a bug that would then be
    reported at the traversal rather than where it happened. Django takes the
    same position for *forward* relations, which go through ``_base_manager``
    precisely so a related object is always retrievable.

    **A write that names its organization is not scoped either.**
    ``create`` / ``get_or_create`` / ``update_or_create`` / ``bulk_create`` are
    copied onto the manager from ``QuerySet``, so they route through
    ``get_queryset()`` and would inherit the refusal -- but the scope they refuse
    to resolve has no effect on what they do. ``QuerySet.create`` does not carry
    the queryset's filters onto the new row and ``bulk_create`` takes
    fully-built instances, so refusing ``Calendar.objects.create(organization=org,
    ...)`` -- which is how essentially every write in this codebase is spelled,
    and which the manager this replaces *required* -- would reject a statement
    that is already unambiguous. A write that does *not* name an organization
    still goes through the scoped queryset: either the context supplies one (and
    ``SingleOrganizationModelMixin.save()`` stamps it) or nothing does and it
    raises, exactly as a read would.
    """

    #: The ways a caller can name the organization on a write.
    #: ``organization_id`` is accepted because ``create(organization_id=...)``
    #: is as explicit as passing the instance and is used throughout the
    #: services.
    _ORGANIZATION_KWARGS = ("organization", "organization_id")

    def get_queryset(self, *args: Any, **kwargs: Any) -> QuerySet:
        # ``instance`` is set by the related managers Django generates in
        # ``create_reverse_many_to_one_manager`` / ``create_forward_many_to_many_manager``
        # and is never present on a plain model manager.
        if getattr(self, "instance", None) is not None:
            return self.get_original_queryset(*args, **kwargs)
        return super().get_queryset(*args, **kwargs)

    def unscoped_default_queryset(self) -> QuerySet:
        """This manager's default view with the *organization* scoping removed.

        Distinct from ``unscoped()``, which means "every row, no filter of any
        kind". A subclass that narrows ``get_queryset`` for a reason unrelated to
        tenancy (``BlockedTimeManager`` and ``AvailableTimeManager`` hide
        group-slot-scoped rows) overrides this so the narrowing survives.

        Used where an organization cannot meaningfully be resolved but the rest of
        the default view still applies -- see
        ``common.virtual_models.OrganizationScopedVirtualModel``.
        """
        return self.get_original_queryset()

    def _names_an_organization(self, kwargs: dict[str, Any] | None) -> bool:
        if not kwargs:
            return False
        return any(kwargs.get(name) is not None for name in self._ORGANIZATION_KWARGS)

    def create(self, **kwargs: Any) -> Any:
        if self._names_an_organization(kwargs):
            return self.unscoped().create(**kwargs)
        return super().create(**kwargs)

    def get_or_create(self, defaults: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if self._names_an_organization(kwargs) or self._names_an_organization(defaults):
            return self.unscoped().get_or_create(defaults=defaults, **kwargs)
        return super().get_or_create(defaults=defaults, **kwargs)

    def update_or_create(self, defaults: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if self._names_an_organization(kwargs) or self._names_an_organization(defaults):
            return self.unscoped().update_or_create(defaults=defaults, **kwargs)
        return super().update_or_create(defaults=defaults, **kwargs)

    def bulk_create(self, objs: Any, *args: Any, **kwargs: Any) -> Any:
        # Always unscoped: every object carries its own ``organization``, and one
        # that does not fails the column's NOT NULL rather than silently landing
        # in whichever organization happened to be bound. ``save()``'s context
        # fallback is not involved -- ``bulk_create`` does not call it.
        return self.unscoped().bulk_create(objs, *args, **kwargs)

    def bulk_update(self, objs: Any, *args: Any, **kwargs: Any) -> Any:
        # Same reasoning as ``bulk_create``: the statement is addressed to the
        # primary keys of instances the caller already holds, each carrying its own
        # organization, so the queryset's scope adds nothing and refusing to
        # resolve one would reject an unambiguous write.
        return self.unscoped().bulk_update(objs, *args, **kwargs)


def requires_annotation(*annotation_names: Iterable[str]):
    """
    Decorator to enforce the function requires a specific annotation.

    Args:
        annotation_name: The name of the required annotation
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            queryset = args[0]

            # Check if the annotation exists in the QuerySet's annotations
            for annotation_name in annotation_names:
                if annotation_name not in getattr(queryset.query, "annotations", {}):
                    raise ValueError(
                        f"Annotation '{annotation_name}' is required for this function."
                    )
            return func(*args, **kwargs)

        return wrapper

    return decorator
