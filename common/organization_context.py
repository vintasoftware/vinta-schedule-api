"""Storage for the organization the current execution context is bound to.

**Re-export of ``vinta_orgs.state``** (the binding API ``vinta-django-orgs``
ships). Phase 0 of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
introduced this module as a *local* ``contextvars`` implementation that mirrored
the package's public surface name-for-name, so every call site that would
*become* implicitly organization-scoped could bind an organization while the
then-current managers still ignored the binding entirely.

**Phase 2a performed the swap.** From here on this module is a thin re-export
and there is exactly one contextvar in the process — the package's. That matters
because the package's managers, ``SingleOrganizationModelMixin.save()`` and
``scope_queryset_to_current_organization`` all read
``vinta_orgs.state._current_organization``; while this module owned a second,
independent contextvar, every Phase 0 binding was invisible to them, and with
``STRICT_ORGANIZATION_FILTER = True`` those call sites would have raised
``OrganizationNotFoundError`` instead of scoping.

Two consequences worth knowing before you bind an organization:

* ``SingleOrganizationModelMixin.save()`` resolves
  ``get_current_organization() or get_default_organization()`` when a scoped
  instance was built with no ``organization``. Inside an
  ``organization_context(...)`` block that now *adopts* the bound organization
  rather than raising. (``DEFAULT_ORGANIZATION_SLUG`` is ``None``, so the
  second half stays a no-op and an *unbound* save still raises.)
* Binding by slug is lazy (``SimpleLazyObject``), so a block that never touches
  a scoped model pays no query.

Kept as a module rather than asking call sites to import ``vinta_orgs.state``
directly: it is the one import path Phase 0 threaded through every Celery task
and management command, and it documents *this project's* contract with the
package in one place.
"""

from vinta_orgs.state import (
    OrganizationOrSlug,
    OrganizationToken,
    clear_current_organization,
    get_current_organization,
    organization_context,
    reset_current_organization,
    set_current_organization,
)


__all__ = [
    "OrganizationOrSlug",
    "OrganizationToken",
    "clear_current_organization",
    "get_current_organization",
    "organization_context",
    "reset_current_organization",
    "set_current_organization",
]
