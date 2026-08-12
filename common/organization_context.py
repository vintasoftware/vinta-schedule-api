"""Storage for the organization the current execution context is bound to.

**Phase 0 of the vinta-django-orgs migration** (see
``ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md``). This
module is a *temporary, local* implementation of the binding API
``vinta-django-orgs`` ships as ``organizations.state``. As of Phase 1a the
package is installed and this repo's app has been renamed to ``tenancy``
(freeing the ``organizations`` name for the package), but the swap-over to the
package's implementation is deliberately deferred — Phase 1a is a rename with
no behavior change, and importing the package's ``organizations.state`` here
would change what a bound query sees before the manager flip (Phase 2) is
ready for it. It exists so every call site that will *become* implicitly
organization-scoped in Phase 2 can bind an organization today, through one
importable name, while the current managers
(``tenancy.managers.BaseOrganizationModelManager`` /
``tenancy.querysets.BaseOrganizationModelQuerySet``) keep ignoring the binding
entirely and requiring their own explicit ``organization`` filter. That is what
makes this phase behavior-neutral: nothing here changes what any query returns.

The public surface below — ``organization_context``, ``set_current_organization``,
``get_current_organization``, ``clear_current_organization``,
``reset_current_organization``, and the ``OrganizationOrSlug`` / ``OrganizationToken``
type aliases — mirrors ``organizations.state`` in the installed package
(``vinta-django-orgs==0.1.1``) name-for-name and semantics-for-semantics
(binding returns a ``contextvars.Token``; nested/sequential binds restore the
*previous* organization rather than clearing it), so a later phase can swap
this module's *body* to a one-line re-export:

    from organizations.state import (  # noqa: F401
        OrganizationOrSlug,
        OrganizationToken,
        clear_current_organization,
        get_current_organization,
        organization_context,
        reset_current_organization,
        set_current_organization,
    )

No call site should need to change at that point — this is why matching the
public API exactly (including accepting an ``Organization`` instance, a slug
string, or a lazily-resolved organization, but never a bare integer id) matters
now, not just for tidiness later.
"""

from __future__ import annotations

import threading
from contextlib import ContextDecorator
from contextvars import ContextVar, Token
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self, cast

from django.utils.functional import LazyObject, SimpleLazyObject


if TYPE_CHECKING:
    from tenancy.models import Organization


#: What callers may bind: a loaded ``Organization``, the slug of one, or a
#: ``LazyObject`` standing in for one -- deliberately not a bare integer id,
#: so every call site already matches the shape ``organization_context`` from
#: the installed package will expect once Phase 1a/2 swaps this module's body.
type OrganizationOrSlug = Organization | LazyObject | str

#: The token :func:`set_current_organization` hands back, which
#: :func:`reset_current_organization` consumes.
type OrganizationToken = Token[Organization | None]

_current_organization: ContextVar[Organization | None] = ContextVar(
    "common.organization_context.current_organization", default=None
)


def _get_organization_by_slug(slug: str) -> Organization | None:
    # Deferred: this module can be imported (by a Celery task/management
    # command module, at worker/command startup) before Django's app
    # registry has finished loading; importing the ``Organization`` model at
    # module scope raises ``AppRegistryNotReady`` in that case (verified).
    from tenancy.models import Organization

    return Organization.objects.filter(slug=slug).first()


def _coerce_organization(organization: OrganizationOrSlug | None) -> Organization | None:
    """Normalize what callers pass into something storable.

    A slug is wrapped in a ``SimpleLazyObject`` so binding an organization by
    slug costs no query until something actually reads it.
    """
    if organization is None or isinstance(organization, LazyObject):
        # Checked before the ``str`` test below and not merged into it:
        # ``isinstance`` matches a ``LazyObject`` on its concrete type, but any
        # other check falls back to the proxied ``__class__`` and would resolve
        # the wrapper here -- forcing the query this laziness exists to avoid.
        return cast("Organization | None", organization)

    if not isinstance(organization, str):
        return organization

    return cast("Organization", SimpleLazyObject(lambda: _get_organization_by_slug(organization)))


def get_current_organization() -> Organization | None:
    """Return the organization bound to the current context, or ``None``."""
    return _current_organization.get()


def set_current_organization(organization: OrganizationOrSlug | None) -> OrganizationToken:
    """Bind ``organization`` (an ``Organization``, a slug, or ``None``) to this context.

    Returns the :class:`contextvars.Token` that restores the previous value
    through :func:`reset_current_organization`. Prefer
    :class:`organization_context` when the binding has a well-defined scope.
    """
    return _current_organization.set(_coerce_organization(organization))


def clear_current_organization() -> OrganizationToken:
    """Unbind the current organization.

    Unlike a ``del`` on a thread map this is a no-op when nothing is bound, so
    teardown code never has to guard the call.
    """
    return _current_organization.set(None)


def reset_current_organization(token: OrganizationToken) -> None:
    """Restore the organization that was bound before ``token`` was issued."""
    _current_organization.reset(token)


class organization_context(ContextDecorator):  # noqa: N801 -- matches the package's public name
    """Bind an organization for a block of code, then restore the previous one.

    Usable as a context manager or as a decorator, which is what makes
    organization-scoped code reachable outside the request/response cycle --
    Celery tasks, management commands, and tests::

        with organization_context(organization):
            Calendar.objects.filter_by_organization(organization.id).count()

        @organization_context(organization)
        def rebuild_index():
            ...

    Nested and sequential uses restore the *previous* organization rather than
    clearing it, so a block never silently unscopes its caller -- a fan-out
    task that binds once per organization inside a loop, for instance, always
    ends the loop back where it started (usually unbound), not stuck on the
    last organization it processed.
    """

    def __init__(self, organization: OrganizationOrSlug | None) -> None:
        self.organization = organization
        self._local = threading.local()

    @property
    def _tokens(self) -> list[OrganizationToken]:
        # Per-thread token stack: tokens are only valid in the context that
        # created them, so one shared list would break under concurrency.
        tokens: list[OrganizationToken] | None = getattr(self._local, "tokens", None)
        if tokens is None:
            tokens = self._local.tokens = []
        return tokens

    def _recreate_cm(self) -> Self:
        # Called once per invocation of a decorated function, so recursive
        # calls each get their own instance instead of sharing a token stack.
        return self.__class__(self.organization)

    def __enter__(self) -> Organization | None:
        self._tokens.append(set_current_organization(self.organization))
        return get_current_organization()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        reset_current_organization(self._tokens.pop())
        return False
