"""The bridge between ``organizations.Organization`` and the scope that bills it.

``vinta-django-billing`` 0.8.0 stopped taking an organization. Every service
method, permission and counter now takes a *scope* -- a row naming whoever pays
-- so that one installation can sell to an organization and to a bare user at
the same time. This project has exactly one kind of payer, so its scope table is
a mirror of its organization table and this module keeps the two in step.

Two jobs:

1. :func:`scope_for` -- the org-to-scope translation every call site needs.
2. :func:`sync_scope_for_organization` -- provisioning, wired to ``post_save``
   so all four organization-creation paths are covered by construction rather
   than by remembering.

Why a signal rather than a call in ``OrganizationService.create_organization``:
there are four organization-creation paths (see
``organizations/tests/test_organization_creation_billing.py`` for the list --
the REST funnel, the reseller GraphQL mutation's raw ``objects.create``, the
admin, and tenant provisioning), only one of which goes through the service. A
scope that a reseller-created child never gets is not a loud failure; it is a
child that silently bills nowhere. ``post_save`` is the one hook all four share.

**The one gap: ``QuerySet.update()`` sends no signal.** A bulk update of
``Organization.parent`` or ``can_invite_organizations`` leaves the mirror stale,
and the hierarchy then answers against the old tree -- silently, since nothing
raises. No production path does that today (organizations are saved one at a
time, through the service, the admin, or the reseller mutation), and this is
documented rather than defended against because the alternatives are worse: a
periodic reconcile would hide the drift it papers over, and a database trigger
would put the mirror somewhere no reader of this module would think to look. If
a bulk update of either field is ever added, call
:func:`sync_scope_for_organization` for each affected row in the same
transaction.

Most of this module's former bulk went with one foreign key. While
``BILLING_SCOPE_MODEL`` pointed at ``vinta_billing``'s shipped scope, a scope
addressed its payer through a generic key, so "which scope bills this
organization" was a content-type lookup nothing could join --
``organization.billing_scope`` is a reverse one-to-one now, which Django caches
per instance and ``select_related`` can fold away.

What survived is :func:`scope_translation_cache`, and it is worth being clear
that it was never about the generic key. The counters read this project's
organization-keyed tables while the engine hands them scope ids, so the two id
spaces have to be mapped whatever the scope model looks like -- once per
counter, eight times on the usage endpoint, unless something memoizes it.
Removing it on the strength of "the foreign key retired all this" took that
endpoint from 23 queries to 29.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from vinta_billing.conf import get_scope_model


if TYPE_CHECKING:
    from organizations.models import Organization


def scope_for(organization: Organization):
    """The scope that bills ``organization``, creating it if it is not there yet.

    The workhorse: everywhere a billing service used to be handed an
    organization it is handed ``scope_for(organization)`` instead.

    Reads through the reverse one-to-one rather than querying by hand, so a
    caller that already did ``select_related("billing_scope")`` pays nothing at
    all. Django caches that descriptor per instance, which is the memoization
    this used to hand-roll -- :func:`_provision` refreshes the cache whenever it
    changes a mirrored field, so the cache cannot go stale behind a re-parent.

    Creates on miss rather than returning ``None``. A caller reaching this has
    an organization in hand and is about to ask a billing question about it, and
    the only reason a scope would be missing is an organization written by a
    path that bypassed ``post_save`` -- a ``bulk_create``, or a fixture loaded
    with signals muted. Answering the billing question is more useful than
    raising, and the row is the same one the mirror would have made.

    Not used to resolve a *request* -- that is ``SCOPE_RESOLVER``'s job, and it
    deliberately never writes.
    """
    try:
        return organization.billing_scope
    except get_scope_model().DoesNotExist:
        return sync_scope_for_organization(organization)


def sync_scope_for_organization(organization: Organization):
    """Create the organization's scope, and bring the mirrored fields up to date.

    Idempotent, and safe to call on every save: it writes only when something it
    mirrors actually differs, so the common no-op save costs one indexed read
    and nothing else.

    The parent is resolved recursively -- an organization saved before its
    ancestors have scopes still gets a correctly linked chain, because reaching
    for the parent's scope provisions that one too. ``_provision`` guards that
    recursion against a parent cycle: ``Organization.parent`` is user-mutable
    through the Django admin, so ``a -> b -> a`` is reachable, and this runs
    from ``post_save`` -- the save that *closes* the cycle is what would trigger
    it. Unguarded that is a ``RecursionError`` from inside a signal handler,
    which surfaces as a failed save rather than as the ``BillingRootCycleError``
    the hierarchy raises for the same shape. Stopping the walk leaves the scope
    chain mirroring the organization chain, cycle included, which is what lets
    ``resolve_billing_root`` detect and report it.
    """
    return _provision(organization, set())


def _provision(organization: Organization, seen: set[int]):
    """One link of the chain, with its ancestors provisioned first.

    Split out so the cycle-tracking set stays an implementation detail rather
    than a private parameter on a function other modules call.
    """
    scope_model = get_scope_model()

    if organization.pk in seen:
        # Already provisioned on this walk. Return its scope without recursing
        # again -- the parent link was set when it was first visited.
        return scope_model.objects.filter(organization=organization).first()
    seen.add(organization.pk)

    parent_scope = None
    # ``organization.parent`` rather than a filter on the id: the recursion is
    # what guarantees the whole chain exists, not just this one link.
    parent = organization.parent
    if parent is not None:
        parent_scope = _provision(parent, seen)

    scope, created = scope_model.objects.get_or_create(
        organization=organization,
        defaults={
            "label": organization.name,
            "parent": parent_scope,
            "is_reseller_root": bool(organization.can_invite_organizations),
        },
    )
    if created:
        return scope

    # Reconcile the three mirrored fields, writing only what actually differs so
    # an ordinary save costs nothing extra.
    updates: dict[str, Any] = {}
    if scope.parent_id != (parent_scope.pk if parent_scope else None):
        updates["parent"] = parent_scope
    if scope.is_reseller_root != bool(organization.can_invite_organizations):
        updates["is_reseller_root"] = bool(organization.can_invite_organizations)
    if scope.label != organization.name:
        updates["label"] = organization.name

    if updates:
        for field, value in updates.items():
            setattr(scope, field, value)
        scope.save(update_fields=[*updates, "modified"])

    # Refresh the reverse descriptor's cache. `scope_for` reads
    # `organization.billing_scope`, which Django caches on the instance the
    # first time it is touched -- so without this a caller that resolved a
    # scope before a re-parent goes on seeing the old `parent_id`, and the
    # hierarchy answers against a tree that no longer exists. The cycle tests
    # catch exactly that.
    organization.billing_scope = scope
    return scope


@receiver(
    post_save,
    # A string rather than the class: ``ModelSignal`` resolves it lazily through
    # the app registry, so this module does not import ``organizations.models``
    # -- which imports this one back, from its branding gate.
    sender="organizations.Organization",
    dispatch_uid="payments_sync_billing_scope",
)
def _sync_scope_on_organization_save(sender, instance, **kwargs) -> None:
    """Keep the mirror in step with every organization write.

    Fires on update as well as create, because ``parent`` and
    ``can_invite_organizations`` are both mutable: promoting an organization to
    a reseller has to move it to being its own billing root, and re-parenting
    one has to move which ceiling its usage pools into. Both are the kind of
    change made once and relied on for years, so drifting here is worse than the
    cost of the check.
    """
    sync_scope_for_organization(instance)


def organization_for(scope) -> "Organization | None":
    """The organization a scope bills.

    The inverse of :func:`scope_for`, for the places holding a scope that need
    the organization back. Optional only because a caller may pass ``None``.
    """
    return scope.organization if scope is not None else None


def scopes_for(organizations: Iterable[Organization]) -> tuple[list[Any], dict[int, int]]:
    """Scopes for many organizations at once, plus the way back.

    Returns ``(scopes, {scope_pk: organization_pk})``. The bulk counterpart to
    :func:`scope_for`, for the batch entitlement paths whose whole reason for
    existing is answering in two queries rather than two per row.

    Unlike :func:`scope_for` this does not create missing scopes. A batch read is
    the wrong place to provision, and any organization reaching one has been
    saved at least once and so already has a scope from ``post_save``. One
    without is absent from both halves of the result, which the callers already
    treat as "not entitled".
    """
    organizations = list(organizations)
    if not organizations:
        return [], {}
    scopes = list(get_scope_model().objects.filter(organization__in=organizations))
    return scopes, {scope.pk: scope.organization_id for scope in scopes}


#: Request-scoped memo for :func:`organization_ids_for_scope_ids`.
#:
#: A contextvar rather than a module-level dict, for the reason
#: ``vinta_billing.entitlement_cache`` gives for its own: a module-level cache
#: would leak between concurrently-handled requests and -- worse -- across
#: tests, where a rolled-back transaction hands the next one the same primary
#: keys pointing at different organizations.
_scope_translation_cache: contextvars.ContextVar[dict[int, int] | None] = contextvars.ContextVar(
    "scope_translation_cache", default=None
)


@contextlib.contextmanager
def scope_translation_cache():
    """Memoize scope-to-organization translation for the duration of the block.

    Activated per request by
    ``payments.middlewares.BillingScopeTranslationCacheMiddleware``. Worth
    having because the engine asks each registered resource's counter for its
    own breakdown and every one of them translates the *same* pooled scope ids
    -- one query per resource without this, eight on ``GET /billing/usage/``.

    Safe to memoize because the mapping cannot change inside a request: a
    scope's ``organization`` is set when it is created and nothing moves it.

    Re-entrant, so a nested block reuses the outer cache. Outside any block the
    translation still runs, just once per call -- the memo is an optimization,
    never a correctness requirement.
    """
    if _scope_translation_cache.get() is not None:
        yield
        return
    token = _scope_translation_cache.set({})
    try:
        yield
    finally:
        _scope_translation_cache.reset(token)


def organization_ids_for_scope_ids(scope_ids: Iterable[int]) -> dict[int, int]:
    """``{scope_id: organization_id}`` -- the translation the usage counters need.

    The engine indexes usage by scope; this project's own tables are keyed by
    organization. One indexed read over a real foreign key, memoized per
    request so eight counters asking the same question cost one.
    """
    ids = list(scope_ids)
    if not ids:
        return {}

    memo = _scope_translation_cache.get()
    if memo is not None:
        known = {sid: memo[sid] for sid in ids if sid in memo}
        if len(known) == len(ids):
            return known
        ids = [sid for sid in ids if sid not in memo]
    else:
        known = {}

    mapping = dict(known)
    for scope_id, organization_id in (
        get_scope_model().objects.filter(pk__in=ids).values_list("pk", "organization_id")
    ):
        mapping[scope_id] = organization_id
        if memo is not None:
            memo[scope_id] = organization_id
    return mapping
