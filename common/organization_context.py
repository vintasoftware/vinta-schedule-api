"""The organization the current execution context is bound to.

**Phase 2a of the vinta-django-orgs migration** (see
``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``).
This module is now a pure re-export of ``organizations.state``, the binding API
``vinta-django-orgs`` ships.

Phase 0 shipped a *local* ``contextvars`` implementation here that mirrored the
package's public surface name-for-name and semantics-for-semantics, precisely so
this swap could be a one-file change with no call site touched. That local
implementation had to go before any model flipped: it owned its **own**
``ContextVar``, while the package's managers read the package's. Two independent
bindings coexisted, so every Phase 0 binding (Celery tasks, management commands)
would have been invisible to the managers the moment they started scoping
implicitly -- silently returning nothing without ``STRICT_ORGANIZATION_FILTER``,
and raising with it. Unifying them here is what makes Phase 0's work count.

Call sites keep importing from this module rather than from ``organizations``
directly: the name ``organizations`` belongs to the third-party package in this
repo (ours was renamed to ``tenancy`` in Phase 1a), and one project-local import
path keeps that distinction from having to be re-learned at every call site.

One consequence of the unification, deliberate and recorded here because it is a
behavior change rather than a refactor: ``SingleOrganizationModelMixin.save()``
falls back to ``get_current_organization() or get_default_organization()`` when a
scoped row is saved with no organization set. That fallback now reads the same
contextvar this module binds, so a row constructed without an explicit
``organization=`` inside an ``organization_context(...)`` block adopts the bound
organization instead of raising. ``DEFAULT_ORGANIZATION_SLUG`` is ``None`` (see
``vinta_schedule_api/settings/base.py``), so the *default*-organization half of
that fallback stays a no-op -- an unbound save with no organization still raises
``OrganizationNotFoundError``.
"""

from organizations.state import (
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
