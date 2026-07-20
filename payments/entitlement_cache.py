"""A per-request memo for boolean entitlement checks.

``EntitlementService.has_entitlement`` costs a billing-root walk (a query per level of
the ``parent`` chain) plus a subscription fetch plus an entitlement-row fetch. Phase 6c
puts it on the two hottest surfaces in the public API:

- ``PublicApiSystemUserMiddleware`` checks ``partner_api`` on **every** GraphQL request.
- ``resolve_branding_for_display`` runs on ``brandingForTenant``, an *unauthenticated*
  public query whose ``tenant_id`` is attacker-supplied.

Both are answering the same question repeatedly within one request. This memo makes the
second and later asks free.

**Scope is opt-in and explicit.** The cache only exists inside an
``entitlement_request_cache()`` block; outside one, ``has_entitlement_cached`` is a plain
pass-through to the service. That is the point: a process-lifetime or thread-local cache
would go stale across Celery tasks and management commands, where a plan change made
mid-process must be visible immediately. A request is short enough that an entitlement
cannot meaningfully change inside it — and if it did, the request already read the old
value once.

Backed by a ``ContextVar``, so it is correct under ASGI/async views and never leaks
between concurrently-handled requests the way a module-level dict would.
"""

import contextlib
import contextvars
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from organizations.models import Organization
    from payments.services.entitlement_service import EntitlementService


_entitlement_cache: contextvars.ContextVar[dict[tuple[int, str], bool] | None] = (
    contextvars.ContextVar("entitlement_request_cache", default=None)
)


@contextlib.contextmanager
def entitlement_request_cache():
    """Activate the memo for the duration of the block.

    Re-entrant: a nested block reuses the outer cache rather than shadowing it, so a
    middleware-scoped cache is not silently discarded by an inner helper.
    """
    if _entitlement_cache.get() is not None:
        yield
        return
    token = _entitlement_cache.set({})
    try:
        yield
    finally:
        _entitlement_cache.reset(token)


def has_entitlement_cached(
    entitlement_service: "EntitlementService",
    organization: "Organization",
    entitlement_key: str,
) -> bool:
    """``entitlement_service.has_entitlement``, memoized when a cache is active.

    Keyed on the *asked-for* organization rather than its billing root: resolving the
    root is itself part of the cost being avoided.
    """
    cache = _entitlement_cache.get()
    if cache is None:
        return entitlement_service.has_entitlement(organization, entitlement_key)

    key = (organization.pk, entitlement_key)
    if key not in cache:
        cache[key] = entitlement_service.has_entitlement(organization, entitlement_key)
    return cache[key]
