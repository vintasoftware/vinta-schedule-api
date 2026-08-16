"""Storage for the organization the current execution context is bound to.

**This project's specialization of ``vinta_orgs.state``** (the binding API
``vinta-django-orgs`` ships). Phase 0 of the vinta-django-orgs migration
(``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``)
introduced this module as a *local* ``contextvars`` implementation that mirrored
the package's public surface name-for-name, so every call site that would
*become* implicitly organization-scoped could bind an organization while the
then-current managers still ignored the binding entirely.

**Phase 2a performed the swap**, and there is exactly one contextvar in the
process -- the package's. That matters because the package's managers,
``SingleOrganizationModelMixin.save()`` and
``scope_queryset_to_current_organization`` all read
``vinta_orgs._state._current_organization``; while this module owned a second,
independent contextvar, every Phase 0 binding was invisible to them, and with
``STRICT_ORGANIZATION_FILTER = True`` those call sites would have raised
``OrganizationNotFoundError`` instead of scoping.

**Phase 3 turned the re-export into a specialization.** Package ``0.4.0``
deleted the module-level ``get_current_organization`` / ``set_current_organization``
/ ``clear_current_organization`` / ``reset_current_organization`` /
``organization_context`` functions and replaced them with
``OrganizationState``, a generic bound once per project to the concrete
organization model. :class:`ProjectOrganizationState` is that binding, and the
five functions below are thin shims over the single instance of it, so the
~30 modules that import them from here did not have to change -- which is the
reason this module exists at all rather than every call site importing
``vinta_orgs.state``. Keep it that way: one project-owned module per package
concern, and call sites import ours.

Two consequences worth knowing before you bind an organization:

* ``SingleOrganizationModelMixin.save()`` resolves the bound organization (then
  ``get_default_organization()``) when a scoped instance was built with no
  ``organization``. Inside an ``organization_context(...)`` block that *adopts*
  the bound organization rather than raising. (``DEFAULT_ORGANIZATION_SLUG`` is
  ``None``, so the second half stays a no-op and an *unbound* save still raises.)
* Binding by slug is lazy (``SimpleLazyObject``), so a block that never touches
  a scoped model pays no query.
"""

from __future__ import annotations

from contextvars import Token
from typing import TYPE_CHECKING

from django.utils.functional import LazyObject

from vinta_orgs.models import AbstractOrganization
from vinta_orgs.state import OrganizationState

from organizations.models import Organization


if TYPE_CHECKING:
    from vinta_orgs.state import OrganizationContext


# The package annotates its generic surface against ``AbstractOrganization``,
# the base every configured model shares -- so without a specialization every
# one of these would hand callers the abstract type while returning an
# ``organizations.Organization`` at runtime. Restating the aliases here in terms
# of *our* model is what makes the ``ORGANIZATION_MODEL`` swap invisible to
# callers, which was the whole point of routing them through this module.
type OrganizationOrSlug = Organization | LazyObject | str

# The one alias deliberately *not* restated in terms of ``Organization``.
# ``contextvars.Token`` is invariant in its parameter and the token comes from
# the package's single, model-agnostic ``ContextVar``, so narrowing it here
# would be a claim the runtime object cannot honour. Callers only ever pass a
# token straight back to :func:`reset_current_organization`, so the parameter is
# opaque to them either way.
type OrganizationToken = Token[AbstractOrganization | None]


class ProjectOrganizationState(OrganizationState[Organization]):
    """The organization context of this project, bound to ``organizations.Organization``.

    Declared once. ``model_class`` is checked against ``ORGANIZATION_MODEL`` when
    the instance below is constructed, so a settings change that stopped pointing
    at this model fails at import rather than by handing out the wrong class.
    """

    model_class = Organization


#: The single state object for this process. Everything below delegates to it,
#: and code that prefers the object form may use it directly.
organization_state = ProjectOrganizationState()


def get_current_organization() -> Organization | None:
    """Return the organization bound to the current context, if any."""
    return organization_state.get()


def set_current_organization(organization: OrganizationOrSlug | None) -> OrganizationToken:
    """Bind ``organization`` (an instance, a lazy instance, or a slug) to this context."""
    return organization_state.set(organization)


def clear_current_organization() -> OrganizationToken:
    """Unbind whatever organization this context had bound."""
    return organization_state.clear()


def reset_current_organization(token: OrganizationToken) -> None:
    """Restore the binding that was in place before ``token`` was issued."""
    organization_state.reset(token)


def organization_context(
    organization: OrganizationOrSlug | None,
) -> OrganizationContext[Organization]:
    """Bind ``organization`` for a ``with`` block or a decorated callable.

    Nesting restores the *previous* binding rather than clearing, and each
    invocation of a decorated function gets its own token stack, so a recursive
    call cannot pop its caller's token.
    """
    return organization_state.context(organization)


__all__ = [
    "OrganizationOrSlug",
    "OrganizationToken",
    "ProjectOrganizationState",
    "clear_current_organization",
    "get_current_organization",
    "organization_context",
    "organization_state",
    "reset_current_organization",
    "set_current_organization",
]
